import io
import os
import re
import secrets
import threading
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as pg_pool
import cloudinary
import cloudinary.uploader

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 60 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "troque-em-producao")
app.permanent_session_lifetime = timedelta(hours=8)

DATABASE_URL = os.environ.get("DATABASE_URL")
MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD", "master123")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov", "webm"}
MAX_VIDEO_SIZE = 40 * 1024 * 1024

if MASTER_PASSWORD == "master123":
    print("[AVISO DE SEGURANÇA] MASTER_PASSWORD não definida no ambiente — usando o padrão inseguro. Defina no Railway!")
if app.secret_key == "troque-em-producao":
    print("[AVISO DE SEGURANÇA] SECRET_KEY não definida no ambiente — sessões podem ser forjadas. Defina no Railway!")

# Identificador da versão em produção — muda a cada deploy. Prefere o SHA
# do commit (Railway); o fallback usa o mtime deste arquivo, que é estável
# entre workers do gunicorn (um timestamp de import divergiria entre
# workers e faria as TVs recarregarem em loop). Usado pelo painel de TV
# para detectar versão nova e se recarregar sozinho.
APP_VERSION = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or str(int(os.path.getmtime(__file__)))

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)

_pool = None

def get_pool():
    global _pool
    if _pool is None:
        _pool = pg_pool.SimpleConnectionPool(
            1, 10,
            DATABASE_URL,
            cursor_factory=RealDictCursor
        )
    return _pool

def get_conn():
    return get_pool().getconn()

def query(sql, params=None, fetch=None):
    conn = get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
    except Exception as e:
        print(f"[DB ERROR] {e}")
        raise
    finally:
        get_pool().putconn(conn)

def init_db():
    query("""ALTER TABLE profissionais ADD COLUMN IF NOT EXISTS qr_tipo TEXT DEFAULT 'whatsapp'""")
    query("""ALTER TABLE profissionais ADD COLUMN IF NOT EXISTS video_url TEXT DEFAULT ''""")
    query("""ALTER TABLE academias ADD COLUMN IF NOT EXISTS fonte TEXT DEFAULT 'Syne'""")
    query("""ALTER TABLE academias ADD COLUMN IF NOT EXISTS exibir_nome BOOLEAN DEFAULT TRUE""")
    query("""ALTER TABLE academias ADD COLUMN IF NOT EXISTS texto_header TEXT DEFAULT 'EQUIPE DE PROFISSIONAIS'""")
    query("""ALTER TABLE academias ADD COLUMN IF NOT EXISTS cards_por_pagina INTEGER DEFAULT 10""")
    query("""ALTER TABLE academias ADD COLUMN IF NOT EXISTS duracao_pagina INTEGER DEFAULT 10""")
    query("""ALTER TABLE academias ADD COLUMN IF NOT EXISTS estilo_foto TEXT DEFAULT 'circulo'""")
    query("""ALTER TABLE academias ADD COLUMN IF NOT EXISTS cor_fundo TEXT DEFAULT '#f0f2f5'""")
    query("""ALTER TABLE academias ADD COLUMN IF NOT EXISTS cor_card TEXT DEFAULT '#ffffff'""")
    query("""ALTER TABLE academias ADD COLUMN IF NOT EXISTS efeito_foto TEXT DEFAULT 'nenhum'""")
    query("""ALTER TABLE academias ALTER COLUMN nome DROP NOT NULL""")
    query("""CREATE TABLE IF NOT EXISTS academias (
        id SERIAL PRIMARY KEY,
        slug TEXT UNIQUE NOT NULL,
        nome TEXT NOT NULL,
        subtitulo TEXT DEFAULT '',
        logo_url TEXT DEFAULT '',
        logo_texto TEXT DEFAULT '',
        cor_primaria TEXT DEFAULT '#1a1a2e',
        cor_destaque TEXT DEFAULT '#e94560',
        cor_tag TEXT DEFAULT '#1a1a2e',
        cor_fundo TEXT DEFAULT '#f0f2f5',
        cor_card TEXT DEFAULT '#ffffff',
        efeito_foto TEXT DEFAULT 'nenhum',
        cta_texto TEXT DEFAULT 'Agende uma avaliacao',
        email_qr TEXT DEFAULT '',
        fonte TEXT DEFAULT 'Syne',
        cards_por_pagina INTEGER DEFAULT 10,
        duracao_pagina INTEGER DEFAULT 10,
        estilo_foto TEXT DEFAULT 'circulo',
        senha_hash TEXT NOT NULL,
        criado_em TIMESTAMP DEFAULT NOW())""")
    query("""CREATE TABLE IF NOT EXISTS profissionais (
        id SERIAL PRIMARY KEY,
        academia_id INTEGER REFERENCES academias(id) ON DELETE CASCADE,
        nome TEXT NOT NULL,
        cargo TEXT DEFAULT '',
        email TEXT DEFAULT '',
        instagram TEXT DEFAULT '',
        whatsapp TEXT DEFAULT '',
        anos TEXT DEFAULT '',
        especialidades TEXT DEFAULT '',
        foto_url TEXT DEFAULT '',
        video_url TEXT DEFAULT '',
        cor_avatar TEXT DEFAULT '#1a6fd4',
        qr_tipo TEXT DEFAULT 'whatsapp',
        ordem INTEGER DEFAULT 0,
        criado_em TIMESTAMP DEFAULT NOW())""")
    query("""CREATE TABLE IF NOT EXISTS scans (
        id SERIAL PRIMARY KEY,
        profissional_id INTEGER REFERENCES profissionais(id) ON DELETE CASCADE,
        academia_id INTEGER REFERENCES academias(id) ON DELETE CASCADE,
        criado_em TIMESTAMP DEFAULT NOW())""")

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

def arquivo_permitido(f):
    return "." in f and f.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def video_permitido(f):
    return "." in f and f.rsplit(".", 1)[1].lower() in ALLOWED_VIDEO_EXTENSIONS

def tamanho_valido(file, max_size=MAX_FILE_SIZE):
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    return size <= max_size

def remover_fundo_bytes(file_bytes):
    """Remove o fundo da imagem localmente com rembg (biblioteca Python,
    roda no próprio servidor, sem conta/API externa). Retorna os bytes
    do PNG resultante (com transparência), ou None se falhar -- nesse
    caso quem chamou deve seguir com a foto original, sem travar o
    cadastro do profissional."""
    try:
        from rembg import remove as rembg_remove
        return rembg_remove(file_bytes)
    except Exception as e:
        print(f"[rembg] falha ao remover fundo: {e}")
        return None

def upload_imagem(file, pasta="painel_tv"):
    # Guarda a foto em alta resolução, sem recorte fixo: o recorte
    # (crop de rosto) e o tamanho de entrega são calculados on-the-fly
    # pelo painel (ver cloudinaryPhotoUrl() em painel.html), ajustados à
    # densidade de pixels de cada TV. Isso evita reenvio de fotos toda
    # vez que o layout muda de tamanho.
    #
    # Antes de subir, tenta remover o fundo com rembg (local, sem
    # depender de nenhum serviço externo). Se falhar, sobe a foto
    # original sem quebrar o cadastro -- só essa foto específica fica
    # sem o fundo removido no estilo "Destaque".
    upload_source = file
    try:
        file.seek(0)
        fundo_removido = remover_fundo_bytes(file.read())
        file.seek(0)
        if fundo_removido:
            upload_source = io.BytesIO(fundo_removido)
    except Exception as e:
        print(f"[rembg] erro lendo arquivo para remoção de fundo: {e}")

    try:
        resultado = cloudinary.uploader.upload(
            upload_source,
            folder=pasta,
            transformation=[{"width": 1600, "height": 1600, "crop": "limit", "quality": "auto:best"}]
        )
        return resultado.get("secure_url", "")
    except Exception as e:
        print(f"Erro upload Cloudinary: {e}")
        return ""

def upload_video(file, pasta="painel_tv/videos"):
    try:
        resultado = cloudinary.uploader.upload(
            file,
            folder=pasta,
            resource_type="video",
            transformation=[{"duration": "90"}]
        )
        return resultado.get("secure_url", "")
    except Exception as e:
        print(f"Erro upload video Cloudinary: {e}")
        return ""

def upload_logo(file, pasta="painel_tv/logos"):
    try:
        resultado = cloudinary.uploader.upload(
            file,
            folder=pasta,
            transformation=[{"height": 200, "crop": "fit"}]
        )
        return resultado.get("secure_url", "")
    except Exception as e:
        print(f"Erro upload logo Cloudinary: {e}")
        return ""

def excluir_do_cloudinary(url, resource_type="image"):
    """Remove do Cloudinary o arquivo apontado pela URL (foto/logo/vídeo
    substituído ou removido), para não acumular arquivos órfãos."""
    if not url or "res.cloudinary.com" not in url:
        return
    try:
        m = re.search(r"/upload/(?:v\d+/)?(.+?)(?:\.\w+)?$", url)
        if m:
            cloudinary.uploader.destroy(m.group(1), resource_type=resource_type)
    except Exception as e:
        print(f"Erro ao excluir do Cloudinary: {e}")

SESSION_TIMEOUT = timedelta(hours=2)

# Rate-limit simples de login, em memória por worker: 5 falhas em 15 min
# bloqueiam novas tentativas daquele IP para aquele alvo (slug ou master).
LOGIN_MAX_TENTATIVAS = 5
LOGIN_JANELA_SEGUNDOS = 15 * 60
_tentativas_login = {}

def _ip_cliente():
    encaminhado = request.headers.get("X-Forwarded-For", "")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    return request.remote_addr or "?"

def login_bloqueado(alvo):
    chave = f"{alvo}|{_ip_cliente()}"
    agora = datetime.utcnow().timestamp()
    recentes = [t for t in _tentativas_login.get(chave, [])
                if agora - t < LOGIN_JANELA_SEGUNDOS]
    _tentativas_login[chave] = recentes
    return len(recentes) >= LOGIN_MAX_TENTATIVAS

def registrar_falha_login(alvo):
    chave = f"{alvo}|{_ip_cliente()}"
    _tentativas_login.setdefault(chave, []).append(datetime.utcnow().timestamp())

def limpar_falhas_login(alvo):
    _tentativas_login.pop(f"{alvo}|{_ip_cliente()}", None)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("academia_slug") != kwargs.get("slug"):
            return redirect(url_for("admin_login", slug=kwargs.get("slug")))
        last = session.get("last_activity")
        now = datetime.utcnow().timestamp()
        if last and isinstance(last, (int, float)) and now - last > SESSION_TIMEOUT.total_seconds():
            session.clear()
            flash("Sessão expirada. Faça login novamente.")
            return redirect(url_for("admin_login", slug=kwargs.get("slug")))
        session["last_activity"] = now
        return f(*args, **kwargs)
    return decorated

def master_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("master_logged"):
            return redirect(url_for("master_login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/__version")
def versao():
    return {"version": APP_VERSION}

@app.route("/<slug>/")
def painel(slug):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    if not academia:
        return "Academia nao encontrada.", 404
    profissionais = query(
        "SELECT * FROM profissionais WHERE academia_id = %s ORDER BY LOWER(nome)",
        (academia["id"],), fetch="all"
    )
    return render_template("painel.html", academia=academia,
        profissionais=profissionais or [], app_version=APP_VERSION)

@app.route("/<slug>/admin", methods=["GET", "POST"])
def admin_login(slug):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    if not academia:
        return "Academia nao encontrada.", 404
    if request.method == "POST":
        if login_bloqueado(slug):
            flash("Muitas tentativas. Aguarde 15 minutos e tente de novo.")
            return render_template("admin_login.html", academia=academia)
        if check_password_hash(academia["senha_hash"], request.form.get("senha", "")):
            limpar_falhas_login(slug)
            session.permanent = True
            session["academia_slug"] = slug
            session["academia_id"] = academia["id"]
            session["last_activity"] = datetime.utcnow().timestamp()
            return redirect(url_for("admin_editor", slug=slug))
        registrar_falha_login(slug)
        flash("Senha incorreta.")
    return render_template("admin_login.html", academia=academia)

@app.route("/<slug>/admin/logout")
def admin_logout(slug):
    session.clear()
    return redirect(url_for("admin_login", slug=slug))

@app.route("/<slug>/admin/editor")
@login_required
def admin_editor(slug):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    profissionais = query(
        """SELECT p.*,
            (SELECT COUNT(*) FROM scans s WHERE s.profissional_id = p.id) AS scans_total,
            (SELECT COUNT(*) FROM scans s WHERE s.profissional_id = p.id
                AND s.criado_em >= date_trunc('month', NOW())) AS scans_mes
        FROM profissionais p WHERE p.academia_id = %s ORDER BY LOWER(p.nome)""",
        (academia["id"],), fetch="all"
    )
    return render_template("admin_editor.html", academia=academia, profissionais=profissionais or [])

@app.route("/<slug>/admin/salvar-config", methods=["POST"])
@login_required
def salvar_config(slug):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    logo_url = academia["logo_url"]
    if "logo" in request.files:
        file = request.files["logo"]
        if file and file.filename and arquivo_permitido(file.filename):
            if not tamanho_valido(file):
                flash("A logo não pode ter mais de 5MB.")
                return redirect(url_for("admin_editor", slug=slug))
            nova_logo = upload_logo(file)
            if nova_logo:
                excluir_do_cloudinary(logo_url)
                logo_url = nova_logo
    fontes_validas = {"Syne","Montserrat","Poppins","Oswald","Bebas Neue","Space Grotesk","Encode Sans","Raleway"}
    fonte = request.form.get("fonte", "Syne")
    if fonte not in fontes_validas:
        fonte = "Syne"
    exibir_nome = request.form.get("exibir_nome") == "on"

    estilo_foto = request.form.get("estilo_foto", "circulo")
    if estilo_foto not in ("circulo", "destaque"):
        estilo_foto = "circulo"

    efeito_foto = request.form.get("efeito_foto", "nenhum")
    if efeito_foto not in ("nenhum", "pb", "vinheta", "duotone", "vibrante"):
        efeito_foto = "nenhum"

    try:
        cards_por_pagina = int(request.form.get("cards_por_pagina", 10))
    except (TypeError, ValueError):
        cards_por_pagina = 10
    cards_por_pagina = max(1, min(30, cards_por_pagina))

    try:
        duracao_pagina = int(request.form.get("duracao_pagina", 10))
    except (TypeError, ValueError):
        duracao_pagina = 10
    duracao_pagina = max(3, min(120, duracao_pagina))

    query("""UPDATE academias SET
        nome=%s, subtitulo=%s, logo_url=%s, logo_texto=%s,
        cor_primaria=%s, cor_destaque=%s, cor_tag=%s, cor_fundo=%s, cor_card=%s,
        cta_texto=%s, email_qr=%s, fonte=%s, exibir_nome=%s, texto_header=%s,
        cards_por_pagina=%s, duracao_pagina=%s, estilo_foto=%s, efeito_foto=%s WHERE slug=%s""",
        (request.form.get("nome"), request.form.get("subtitulo"), logo_url,
         request.form.get("logo_texto"), request.form.get("cor_primaria"),
         request.form.get("cor_destaque"), request.form.get("cor_tag"),
         request.form.get("cor_fundo", "#f0f2f5"),
         request.form.get("cor_card", "#ffffff"),
         request.form.get("cta_texto"), request.form.get("email_qr"),
         fonte, exibir_nome, request.form.get("texto_header", "EQUIPE DE PROFISSIONAIS"),
         cards_por_pagina, duracao_pagina, estilo_foto, efeito_foto, slug))
    flash("Configuracoes salvas!")
    return redirect(url_for("admin_editor", slug=slug))


@app.route("/<slug>/admin/trocar-senha", methods=["POST"])
@login_required
def trocar_senha(slug):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    senha_atual = request.form.get("senha_atual", "")
    senha_nova = request.form.get("senha_nova", "")
    senha_confirmacao = request.form.get("senha_confirmacao", "")
    if not check_password_hash(academia["senha_hash"], senha_atual):
        flash("Senha atual incorreta.")
        return redirect(url_for("admin_editor", slug=slug) + "#config")
    if len(senha_nova) < 6:
        flash("A nova senha deve ter pelo menos 6 caracteres.")
        return redirect(url_for("admin_editor", slug=slug) + "#config")
    if senha_nova != senha_confirmacao:
        flash("As senhas não coincidem.")
        return redirect(url_for("admin_editor", slug=slug) + "#config")
    query("UPDATE academias SET senha_hash=%s WHERE slug=%s",
          (generate_password_hash(senha_nova), slug))
    flash("Senha alterada com sucesso!")
    return redirect(url_for("admin_editor", slug=slug))

@app.route("/<slug>/admin/remover-logo", methods=["POST"])
@login_required
def remover_logo(slug):
    academia = query("SELECT logo_url FROM academias WHERE slug=%s", (slug,), fetch="one")
    if academia:
        excluir_do_cloudinary(academia["logo_url"])
    query("UPDATE academias SET logo_url='' WHERE slug=%s", (slug,))
    flash("Logo removida.")
    return redirect(url_for("admin_editor", slug=slug))
@app.route("/<slug>/admin/profissional/adicionar", methods=["POST"])
@login_required
def adicionar_profissional(slug):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    foto_url = ""
    if "foto" in request.files:
        file = request.files["foto"]
        if file and file.filename and arquivo_permitido(file.filename):
            if not tamanho_valido(file):
                flash("A foto não pode ter mais de 5MB.")
                return redirect(url_for("admin_editor", slug=slug))
            foto_url = upload_imagem(file, pasta="painel_tv/profissionais")
    video_url = ""
    if "video" in request.files:
        file = request.files["video"]
        if file and file.filename:
            if not video_permitido(file.filename):
                flash("Formato de vídeo não suportado. Use MP4, MOV ou WEBM.")
                return redirect(url_for("admin_editor", slug=slug))
            if not tamanho_valido(file, MAX_VIDEO_SIZE):
                flash("O vídeo não pode ter mais de 40MB.")
                return redirect(url_for("admin_editor", slug=slug))
            video_url = upload_video(file)
    qr_tipo = request.form.get("qr_tipo", "whatsapp")
    if qr_tipo not in ("whatsapp", "instagram", "ambos"):
        qr_tipo = "whatsapp"
    query("""INSERT INTO profissionais
        (academia_id, nome, cargo, email, instagram, whatsapp, anos, especialidades, foto_url, video_url, cor_avatar, qr_tipo, ordem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
        (SELECT COALESCE(MAX(ordem),0)+1 FROM profissionais WHERE academia_id=%s))""",
        (academia["id"], request.form.get("nome"), request.form.get("cargo"),
         request.form.get("email"), request.form.get("instagram"),
         request.form.get("whatsapp", ""), request.form.get("anos"),
         request.form.get("especialidades"), foto_url, video_url,
         request.form.get("cor_avatar", "#1a6fd4"), qr_tipo, academia["id"]))
    flash("Profissional adicionado!")
    return redirect(url_for("admin_editor", slug=slug))

@app.route("/<slug>/admin/profissional/<int:prof_id>/editar", methods=["POST"])
@login_required
def editar_profissional(slug, prof_id):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    prof = query("SELECT * FROM profissionais WHERE id=%s AND academia_id=%s",
        (prof_id, academia["id"]), fetch="one")
    if not prof:
        flash("Profissional nao encontrado.")
        return redirect(url_for("admin_editor", slug=slug))
    foto_url = prof["foto_url"]
    if "foto" in request.files:
        file = request.files["foto"]
        if file and file.filename and arquivo_permitido(file.filename):
            if not tamanho_valido(file):
                flash("A foto não pode ter mais de 5MB.")
                return redirect(url_for("admin_editor", slug=slug))
            nova_foto = upload_imagem(file, pasta="painel_tv/profissionais")
            if nova_foto:
                excluir_do_cloudinary(foto_url)
                foto_url = nova_foto
    video_url = prof["video_url"]
    if request.form.get("remover_video") == "on":
        excluir_do_cloudinary(video_url, resource_type="video")
        video_url = ""
    if "video" in request.files:
        file = request.files["video"]
        if file and file.filename:
            if not video_permitido(file.filename):
                flash("Formato de vídeo não suportado. Use MP4, MOV ou WEBM.")
                return redirect(url_for("admin_editor", slug=slug))
            if not tamanho_valido(file, MAX_VIDEO_SIZE):
                flash("O vídeo não pode ter mais de 40MB.")
                return redirect(url_for("admin_editor", slug=slug))
            novo_video = upload_video(file)
            if novo_video:
                excluir_do_cloudinary(video_url, resource_type="video")
                video_url = novo_video
    qr_tipo = request.form.get("qr_tipo", "whatsapp")
    if qr_tipo not in ("whatsapp", "instagram", "ambos"):
        qr_tipo = "whatsapp"
    query("""UPDATE profissionais SET
        nome=%s, cargo=%s, email=%s, instagram=%s, whatsapp=%s,
        anos=%s, especialidades=%s, foto_url=%s, video_url=%s, cor_avatar=%s, qr_tipo=%s
        WHERE id=%s AND academia_id=%s""",
        (request.form.get("nome"), request.form.get("cargo"),
         request.form.get("email"), request.form.get("instagram"),
         request.form.get("whatsapp", ""), request.form.get("anos"),
         request.form.get("especialidades"), foto_url, video_url,
         request.form.get("cor_avatar", "#1a6fd4"), qr_tipo, prof_id, academia["id"]))
    flash("Profissional atualizado!")
    return redirect(url_for("admin_editor", slug=slug))

@app.route("/<slug>/admin/profissional/<int:prof_id>/remover", methods=["POST"])
@login_required
def remover_profissional(slug, prof_id):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    prof = query("SELECT foto_url, video_url FROM profissionais WHERE id=%s AND academia_id=%s",
                 (prof_id, academia["id"]), fetch="one")
    if prof:
        excluir_do_cloudinary(prof["foto_url"])
        excluir_do_cloudinary(prof["video_url"], resource_type="video")
    query("DELETE FROM profissionais WHERE id=%s AND academia_id=%s", (prof_id, academia["id"]))
    flash("Profissional removido.")
    return redirect(url_for("admin_editor", slug=slug))

@app.route("/master", methods=["GET", "POST"])
def master_login():
    if session.get("master_logged"):
        return redirect(url_for("master_dashboard"))
    if request.method == "POST":
        if login_bloqueado("__master__"):
            flash("Muitas tentativas. Aguarde 15 minutos e tente de novo.")
            return render_template("master_login.html")
        senha = request.form.get("senha", "")
        if secrets.compare_digest(senha.encode(), MASTER_PASSWORD.encode()):
            limpar_falhas_login("__master__")
            session.permanent = True
            session["master_logged"] = True
            return redirect(url_for("master_dashboard"))
        registrar_falha_login("__master__")
        flash("Senha incorreta.")
    return render_template("master_login.html")

@app.route("/master/logout")
def master_logout():
    session.pop("master_logged", None)
    return redirect(url_for("master_login"))

@app.route("/master/dashboard")
@master_required
def master_dashboard():
    academias = query("""
        SELECT a.*, COUNT(p.id) as total_profs
        FROM academias a
        LEFT JOIN profissionais p ON p.academia_id = a.id
        GROUP BY a.id ORDER BY a.criado_em DESC
    """, fetch="all") or []

    total_profissionais = sum(a["total_profs"] for a in academias)
    now = datetime.now()
    novos_mes = sum(1 for a in academias
        if a["criado_em"] and a["criado_em"].month == now.month
        and a["criado_em"].year == now.year)

    return render_template("master.html",
        academias=academias,
        total_academias=len(academias),
        total_profissionais=total_profissionais,
        novos_mes=novos_mes)

@app.route("/setup", methods=["GET", "POST"])
@master_required
def setup():
    if request.method == "POST":
        slug = request.form.get("slug", "").lower().strip()
        nome = request.form.get("nome", "").strip()
        senha = request.form.get("senha", "").strip()
        if not slug or not nome or not senha:
            flash("Preencha todos os campos.")
            return redirect(url_for("setup"))
        if not re.match(r'^[a-z0-9-]+$', slug):
            flash("Slug so pode ter letras minusculas, numeros e hifen.")
            return redirect(url_for("setup"))
        if query("SELECT id FROM academias WHERE slug=%s", (slug,), fetch="one"):
            flash("Slug ja em uso.")
            return redirect(url_for("setup"))
        query("INSERT INTO academias (slug,nome,senha_hash) VALUES (%s,%s,%s)",
              (slug, nome, generate_password_hash(senha)))
        flash(f"Academia criada! Acesse: /{slug}/ e /{slug}/admin")
        return redirect(url_for("setup"))
    return render_template("setup.html")

@app.route("/<slug>/prof/<int:prof_id>/links")
def prof_links(slug, prof_id):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    if not academia:
        return render_template("404.html"), 404
    prof = query("SELECT * FROM profissionais WHERE id=%s AND academia_id=%s",
                 (prof_id, academia["id"]), fetch="one")
    if not prof:
        return render_template("404.html"), 404
    try:
        query("INSERT INTO scans (profissional_id, academia_id) VALUES (%s, %s)",
              (prof_id, academia["id"]))
    except Exception as e:
        print(f"[SCAN] Falha ao registrar scan: {e}")
    return render_template("prof_links.html", academia=academia, prof=prof)

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("500.html"), 500

@app.errorhandler(413)
def file_too_large(e):
    flash("Arquivo muito grande. Limite: 5MB para fotos e logo, 40MB para vídeos.")
    return redirect(request.referrer or "/")

with app.app_context():
    init_db()

def _aquecer_rembg():
    # Roda uma remoção de fundo "descartável" em segundo plano assim que o
    # servidor sobe, só para forçar o download do modelo de IA (~176MB) e
    # o carregamento do onnxruntime antes que um upload de verdade precise
    # disso -- sem isso, o primeiro upload de foto depois de cada deploy
    # ficaria bem mais lento (download + inferência na mesma requisição).
    try:
        from io import BytesIO
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (32, 32), "white").save(buf, format="PNG")
        remover_fundo_bytes(buf.getvalue())
        print("[rembg] modelo pré-carregado com sucesso")
    except Exception as e:
        print(f"[rembg] aquecimento falhou (upload real ainda deve funcionar): {e}")

threading.Thread(target=_aquecer_rembg, daemon=True).start()

if __name__ == "__main__":
    app.run(debug=False)




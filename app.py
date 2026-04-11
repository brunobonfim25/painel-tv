import os
import re
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
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "troque-em-producao")
app.permanent_session_lifetime = timedelta(hours=8)

DATABASE_URL = os.environ.get("DATABASE_URL")
MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD", "master123")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

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
        cta_texto TEXT DEFAULT 'Agende uma avaliacao',
        email_qr TEXT DEFAULT '',
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
        cor_avatar TEXT DEFAULT '#1a6fd4',
        ordem INTEGER DEFAULT 0,
        criado_em TIMESTAMP DEFAULT NOW())""")

def arquivo_permitido(f):
    return "." in f and f.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def upload_imagem(file, pasta="painel_tv"):
    try:
        resultado = cloudinary.uploader.upload(
            file,
            folder=pasta,
            transformation=[{"width": 400, "height": 400, "crop": "fill", "gravity": "face"}]
        )
        return resultado.get("secure_url", "")
    except Exception as e:
        print(f"Erro upload Cloudinary: {e}")
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

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("academia_slug") != kwargs.get("slug"):
            return redirect(url_for("admin_login", slug=kwargs.get("slug")))
        return f(*args, **kwargs)
    return decorated

def master_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("master_logged"):
            return redirect(url_for("master_login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/<slug>/")
def painel(slug):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    if not academia:
        return "Academia nao encontrada.", 404
    profissionais = query(
        "SELECT * FROM profissionais WHERE academia_id = %s ORDER BY ordem, id",
        (academia["id"],), fetch="all"
    )
    return render_template("painel.html", academia=academia, profissionais=profissionais or [])

@app.route("/<slug>/admin", methods=["GET", "POST"])
def admin_login(slug):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    if not academia:
        return "Academia nao encontrada.", 404
    if request.method == "POST":
        if check_password_hash(academia["senha_hash"], request.form.get("senha", "")):
            session.permanent = True
            session["academia_slug"] = slug
            session["academia_id"] = academia["id"]
            return redirect(url_for("admin_editor", slug=slug))
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
        "SELECT * FROM profissionais WHERE academia_id = %s ORDER BY ordem, id",
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
            logo_url = upload_logo(file)
    query("""UPDATE academias SET
        nome=%s, subtitulo=%s, logo_url=%s, logo_texto=%s,
        cor_primaria=%s, cor_destaque=%s, cor_tag=%s,
        cta_texto=%s, email_qr=%s WHERE slug=%s""",
        (request.form.get("nome"), request.form.get("subtitulo"), logo_url,
         request.form.get("logo_texto"), request.form.get("cor_primaria"),
         request.form.get("cor_destaque"), request.form.get("cor_tag"),
         request.form.get("cta_texto"), request.form.get("email_qr"), slug))
    flash("Configuracoes salvas!")
    return redirect(url_for("admin_editor", slug=slug))


@app.route("/<slug>/admin/remover-logo", methods=["POST"])
@login_required
def remover_logo(slug):
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
            foto_url = upload_imagem(file, pasta="painel_tv/profissionais")
    query("""INSERT INTO profissionais
        (academia_id, nome, cargo, email, instagram, whatsapp, anos, especialidades, foto_url, cor_avatar, ordem)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
        (SELECT COALESCE(MAX(ordem),0)+1 FROM profissionais WHERE academia_id=%s))""",
        (academia["id"], request.form.get("nome"), request.form.get("cargo"),
         request.form.get("email"), request.form.get("instagram"),
         request.form.get("whatsapp", ""), request.form.get("anos"),
         request.form.get("especialidades"), foto_url,
         request.form.get("cor_avatar", "#1a6fd4"), academia["id"]))
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
            foto_url = upload_imagem(file, pasta="painel_tv/profissionais")
    query("""UPDATE profissionais SET
        nome=%s, cargo=%s, email=%s, instagram=%s, whatsapp=%s,
        anos=%s, especialidades=%s, foto_url=%s, cor_avatar=%s
        WHERE id=%s AND academia_id=%s""",
        (request.form.get("nome"), request.form.get("cargo"),
         request.form.get("email"), request.form.get("instagram"),
         request.form.get("whatsapp", ""), request.form.get("anos"),
         request.form.get("especialidades"), foto_url,
         request.form.get("cor_avatar", "#1a6fd4"), prof_id, academia["id"]))
    flash("Profissional atualizado!")
    return redirect(url_for("admin_editor", slug=slug))

@app.route("/<slug>/admin/profissional/<int:prof_id>/remover", methods=["POST"])
@login_required
def remover_profissional(slug, prof_id):
    academia = query("SELECT * FROM academias WHERE slug = %s", (slug,), fetch="one")
    query("DELETE FROM profissionais WHERE id=%s AND academia_id=%s", (prof_id, academia["id"]))
    flash("Profissional removido.")
    return redirect(url_for("admin_editor", slug=slug))

@app.route("/master", methods=["GET", "POST"])
def master_login():
    if session.get("master_logged"):
        return redirect(url_for("master_dashboard"))
    if request.method == "POST":
        if request.form.get("senha") == MASTER_PASSWORD:
            session.permanent = True
            session["master_logged"] = True
            return redirect(url_for("master_dashboard"))
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

with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=False)




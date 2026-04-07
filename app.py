import os
import json
import uuid
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'troque-isso-em-producao')

DATABASE_URL = os.environ.get('DATABASE_URL')
UPLOAD_FOLDER = os.path.join('static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ─── Banco de dados ────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def query(sql, params=None, fetch=None):
    conn = get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                if fetch == 'one':
                    return cur.fetchone()
                if fetch == 'all':
                    return cur.fetchall()
    finally:
        conn.close()


def init_db():
    """Cria as tabelas se não existirem."""
    query("""
        CREATE TABLE IF NOT EXISTS academias (
            id          SERIAL PRIMARY KEY,
            slug        TEXT UNIQUE NOT NULL,
            nome        TEXT NOT NULL,
            subtitulo   TEXT DEFAULT '',
            logo_url    TEXT DEFAULT '',
            logo_texto  TEXT DEFAULT '',
            cor_primaria TEXT DEFAULT '#1a1a2e',
            cor_destaque TEXT DEFAULT '#e94560',
            cor_tag      TEXT DEFAULT '#1a1a2e',
            cta_texto   TEXT DEFAULT 'Agende uma avaliação gratuita',
            email_qr    TEXT DEFAULT '',
            senha_hash  TEXT NOT NULL,
            criado_em   TIMESTAMP DEFAULT NOW()
        )
    """)
    query("""
        CREATE TABLE IF NOT EXISTS profissionais (
            id          SERIAL PRIMARY KEY,
            academia_id INTEGER REFERENCES academias(id) ON DELETE CASCADE,
            nome        TEXT NOT NULL,
            cargo       TEXT DEFAULT '',
            email       TEXT DEFAULT '',
            instagram   TEXT DEFAULT '',
            anos        TEXT DEFAULT '',
            especialidades TEXT DEFAULT '',
            foto_url    TEXT DEFAULT '',
            cor_avatar  TEXT DEFAULT '#1a6fd4',
            ordem       INTEGER DEFAULT 0,
            criado_em   TIMESTAMP DEFAULT NOW()
        )
    """)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def arquivo_permitido(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def login_required(f):
    """Decorator: protege rotas do admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        slug = kwargs.get('slug')
        if session.get('academia_slug') != slug:
            return redirect(url_for('admin_login', slug=slug))
        return f(*args, **kwargs)
    return decorated


# ─── Rota pública — Painel da TV ───────────────────────────────────────────────

@app.route('/<slug>/')
def painel(slug):
    academia = query(
        "SELECT * FROM academias WHERE slug = %s",
        (slug,), fetch='one'
    )
    if not academia:
        return "Academia não encontrada.", 404

    profissionais = query(
        "SELECT * FROM profissionais WHERE academia_id = %s ORDER BY ordem, id",
        (academia['id'],), fetch='all'
    )
    return render_template('painel.html',
                           academia=academia,
                           profissionais=profissionais or [])


# ─── Admin — Login ────────────────────────────────────────────────────────────

@app.route('/<slug>/admin', methods=['GET', 'POST'])
def admin_login(slug):
    academia = query(
        "SELECT * FROM academias WHERE slug = %s",
        (slug,), fetch='one'
    )
    if not academia:
        return "Academia não encontrada.", 404

    if request.method == 'POST':
        senha = request.form.get('senha', '')
        if check_password_hash(academia['senha_hash'], senha):
            session['academia_slug'] = slug
            session['academia_id'] = academia['id']
            return redirect(url_for('admin_editor', slug=slug))
        flash('Senha incorreta. Tente novamente.')

    return render_template('admin_login.html', academia=academia)


@app.route('/<slug>/admin/logout')
def admin_logout(slug):
    session.clear()
    return redirect(url_for('admin_login', slug=slug))


# ─── Admin — Editor ───────────────────────────────────────────────────────────

@app.route('/<slug>/admin/editor')
@login_required
def admin_editor(slug):
    academia = query(
        "SELECT * FROM academias WHERE slug = %s",
        (slug,), fetch='one'
    )
    profissionais = query(
        "SELECT * FROM profissionais WHERE academia_id = %s ORDER BY ordem, id",
        (academia['id'],), fetch='all'
    )
    return render_template('admin_editor.html',
                           academia=academia,
                           profissionais=profissionais or [])


# ─── Admin — Salvar configurações gerais ──────────────────────────────────────

@app.route('/<slug>/admin/salvar-config', methods=['POST'])
@login_required
def salvar_config(slug):
    academia = query(
        "SELECT * FROM academias WHERE slug = %s",
        (slug,), fetch='one'
    )

    logo_url = academia['logo_url']
    if 'logo' in request.files:
        file = request.files['logo']
        if file and file.filename and arquivo_permitido(file.filename):
            filename = f"{slug}_{uuid.uuid4().hex[:8]}_{secure_filename(file.filename)}"
            caminho = os.path.join(UPLOAD_FOLDER, filename)
            file.save(caminho)
            logo_url = '/' + caminho.replace('\\', '/')

    query("""
        UPDATE academias SET
            nome         = %s,
            subtitulo    = %s,
            logo_url     = %s,
            logo_texto   = %s,
            cor_primaria = %s,
            cor_destaque = %s,
            cor_tag      = %s,
            cta_texto    = %s,
            email_qr     = %s
        WHERE slug = %s
    """, (
        request.form.get('nome'),
        request.form.get('subtitulo'),
        logo_url,
        request.form.get('logo_texto'),
        request.form.get('cor_primaria'),
        request.form.get('cor_destaque'),
        request.form.get('cor_tag'),
        request.form.get('cta_texto'),
        request.form.get('email_qr'),
        slug
    ))
    flash('Configurações salvas com sucesso!')
    return redirect(url_for('admin_editor', slug=slug))


# ─── Admin — Adicionar profissional ───────────────────────────────────────────

@app.route('/<slug>/admin/profissional/adicionar', methods=['POST'])
@login_required
def adicionar_profissional(slug):
    academia = query(
        "SELECT * FROM academias WHERE slug = %s",
        (slug,), fetch='one'
    )

    foto_url = ''
    if 'foto' in request.files:
        file = request.files['foto']
        if file and file.filename and arquivo_permitido(file.filename):
            filename = f"prof_{uuid.uuid4().hex[:10]}_{secure_filename(file.filename)}"
            caminho = os.path.join(UPLOAD_FOLDER, filename)
            file.save(caminho)
            foto_url = '/' + caminho.replace('\\', '/')

    query("""
        INSERT INTO profissionais
            (academia_id, nome, cargo, email, instagram, anos, especialidades, foto_url, cor_avatar, ordem)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
            (SELECT COALESCE(MAX(ordem), 0) + 1 FROM profissionais WHERE academia_id = %s))
    """, (
        academia['id'],
        request.form.get('nome'),
        request.form.get('cargo'),
        request.form.get('email'),
        request.form.get('instagram'),
        request.form.get('anos'),
        request.form.get('especialidades'),
        foto_url,
        request.form.get('cor_avatar', '#1a6fd4'),
        academia['id']
    ))
    flash('Profissional adicionado!')
    return redirect(url_for('admin_editor', slug=slug))


# ─── Admin — Editar profissional ──────────────────────────────────────────────

@app.route('/<slug>/admin/profissional/<int:prof_id>/editar', methods=['POST'])
@login_required
def editar_profissional(slug, prof_id):
    academia = query(
        "SELECT * FROM academias WHERE slug = %s",
        (slug,), fetch='one'
    )
    prof = query(
        "SELECT * FROM profissionais WHERE id = %s AND academia_id = %s",
        (prof_id, academia['id']), fetch='one'
    )
    if not prof:
        flash('Profissional não encontrado.')
        return redirect(url_for('admin_editor', slug=slug))

    foto_url = prof['foto_url']
    if 'foto' in request.files:
        file = request.files['foto']
        if file and file.filename and arquivo_permitido(file.filename):
            filename = f"prof_{uuid.uuid4().hex[:10]}_{secure_filename(file.filename)}"
            caminho = os.path.join(UPLOAD_FOLDER, filename)
            file.save(caminho)
            foto_url = '/' + caminho.replace('\\', '/')

    query("""
        UPDATE profissionais SET
            nome           = %s,
            cargo          = %s,
            email          = %s,
            instagram      = %s,
            anos           = %s,
            especialidades = %s,
            foto_url       = %s,
            cor_avatar     = %s
        WHERE id = %s AND academia_id = %s
    """, (
        request.form.get('nome'),
        request.form.get('cargo'),
        request.form.get('email'),
        request.form.get('instagram'),
        request.form.get('anos'),
        request.form.get('especialidades'),
        foto_url,
        request.form.get('cor_avatar', '#1a6fd4'),
        prof_id,
        academia['id']
    ))
    flash('Profissional atualizado!')
    return redirect(url_for('admin_editor', slug=slug))


# ─── Admin — Remover profissional ─────────────────────────────────────────────

@app.route('/<slug>/admin/profissional/<int:prof_id>/remover', methods=['POST'])
@login_required
def remover_profissional(slug, prof_id):
    academia = query(
        "SELECT * FROM academias WHERE slug = %s",
        (slug,), fetch='one'
    )
    query(
        "DELETE FROM profissionais WHERE id = %s AND academia_id = %s",
        (prof_id, academia['id'])
    )
    flash('Profissional removido.')
    return redirect(url_for('admin_editor', slug=slug))


# ─── Rota de setup — cria nova academia (só use 1x por cliente) ───────────────

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    """
    Rota para você criar um novo cliente.
    Acesse: seusite.com/setup
    Depois de criar, comente ou remova essa rota.
    """
    if request.method == 'POST':
        slug  = request.form.get('slug', '').lower().strip()
        nome  = request.form.get('nome', '').strip()
        senha = request.form.get('senha', '').strip()

        if not slug or not nome or not senha:
            flash('Preencha todos os campos.')
            return redirect(url_for('setup'))

        existe = query(
            "SELECT id FROM academias WHERE slug = %s",
            (slug,), fetch='one'
        )
        if existe:
            flash('Esse slug já está em uso. Escolha outro.')
            return redirect(url_for('setup'))

        query("""
            INSERT INTO academias (slug, nome, senha_hash)
            VALUES (%s, %s, %s)
        """, (slug, nome, generate_password_hash(senha)))

        flash(f'Academia criada! Acesse: /{slug}/ e /{slug}/admin')
        return redirect(url_for('setup'))

    return '''
    <style>body{font-family:Arial;max-width:400px;margin:60px auto;padding:20px}
    input{width:100%;padding:8px;margin:6px 0 14px;border:1px solid #ccc;border-radius:4px}
    button{background:#1a1a2e;color:#fff;padding:10px 20px;border:none;border-radius:4px;cursor:pointer;width:100%}
    .msg{background:#eef;padding:10px;border-radius:4px;margin-bottom:14px}</style>
    <h2>Criar nova academia</h2>
    ''' + (''.join(f'<div class="msg">{m}</div>' for m in session.get('_flashes', []))) + '''
    <form method="POST">
      <label>Slug (URL) — ex: bt, fitness, bodytech</label>
      <input name="slug" placeholder="bt" required>
      <label>Nome da academia</label>
      <input name="nome" placeholder="BT Academia" required>
      <label>Senha do admin</label>
      <input type="password" name="senha" required>
      <button type="submit">Criar Academia</button>
    </form>
    '''


# ─── Inicialização ────────────────────────────────────────────────────────────

with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(debug=False)

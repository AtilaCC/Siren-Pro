"""
api/auth.py — Autenticação via JWT.

Endpoints:
  POST /register  → cria usuário
  POST /login     → retorna JWT

Senhas armazenadas com hash werkzeug (pbkdf2:sha256).
"""

import time
import logging
from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash, check_password_hash
from db.connection import get_db

log    = logging.getLogger("SIREN.auth")
auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/register", methods=["POST"])
def register():
    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "username e password obrigatórios"}), 400
    if len(password) < 6:
        return jsonify({"error": "password deve ter no mínimo 6 caracteres"}), 400

    hashed = generate_password_hash(password)
    try:
        db = get_db()
        c  = db.cursor()
        c.execute(
            "INSERT INTO users (username, password, created_at) VALUES (%s, %s, %s) RETURNING id",
            (username, hashed, int(time.time())),
        )
        user_id = c.fetchone()[0]
        db.commit()
        db.close()
        log.info(f"Usuário registrado: {username} (id={user_id})")
        return jsonify({"message": "Usuário criado com sucesso", "user_id": user_id}), 201
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "Username já existe"}), 409
        log.error(f"Erro no registro: {e}")
        return jsonify({"error": "Erro interno"}), 500


@auth_bp.route("/login", methods=["POST"])
def login():
    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "username e password obrigatórios"}), 400

    try:
        db = get_db()
        c  = db.cursor()
        c.execute("SELECT id, password FROM users WHERE username=%s", (username,))
        row = c.fetchone()
        db.close()
    except Exception as e:
        log.error(f"Erro no login: {e}")
        return jsonify({"error": "Erro interno"}), 500

    if not row or not check_password_hash(row[1], password):
        return jsonify({"error": "Credenciais inválidas"}), 401

    token = create_access_token(identity=str(row[0]))
    log.info(f"Login: {username} (id={row[0]})")
    return jsonify({"access_token": token, "user_id": row[0]}), 200

PY := .venv/bin/python
PORT ?= 8787

.PHONY: install build run dev-backend dev-web

install:
	uv venv -q .venv --python 3.12 || true
	uv pip install --python $(PY) -e .
	cd web && npm install --no-audit --no-fund

build:
	cd web && npm run build

run:
	.venv/bin/atrium --port $(PORT)

dev-backend:
	.venv/bin/atrium --port $(PORT) --no-browser --reload

dev-web:
	cd web && ATRIUM_BACKEND=http://127.0.0.1:$(PORT) npm run dev

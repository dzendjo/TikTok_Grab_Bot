PYTHON := python3.14
PIP := pip3.14

.PHONY: help dev dev-docker install install-dev format lint typecheck test clean

help:
	@echo "🤖 Aiogram Bot Template - Команди"
	@echo ""
	@echo "Розробка:"
	@echo "  make dev          - Запустити з hot-reload"
	@echo "  make dev-docker   - Запустити в Docker з hot-reload"
	@echo ""
	@echo "Встановлення:"
	@echo "  make install      - Встановити залежності"
	@echo "  make install-dev  - Встановити dev залежності"
	@echo ""
	@echo "Якість коду:"
	@echo "  make format       - Форматувати код (black)"
	@echo "  make lint         - Перевірити код (ruff)"
	@echo "  make typecheck    - Перевірити типи (mypy)"
	@echo "  make test         - Запустити тести"
	@echo ""
	@echo "Очищення:"
	@echo "  make clean        - Очистити кеш та тимчасові файли"

dev:
	@echo "🔥 Запуск в режимі розробки з hot-reload..."
	$(PYTHON) dev.py

dev-docker:
	@echo "🐳 Запуск Docker dev режиму з автоматичною збіркою..."
	docker-compose -f docker-compose.dev.yml up --build bot

install:
	@echo "📦 Встановлення залежностей..."
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

install-dev:
	@echo "📦 Встановлення dev залежностей..."
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

format:
	@echo "✨ Форматування коду..."
	$(PYTHON) -m black app/

lint:
	@echo "🔍 Перевірка коду..."
	$(PYTHON) -m ruff check app/

lint-fix:
	@echo "🔧 Виправлення коду..."
	$(PYTHON) -m ruff check --fix app/

typecheck:
	@echo "🔎 Перевірка типів..."
	$(PYTHON) -m mypy app/

test:
	@echo "🧪 Запуск тестів..."
	$(PYTHON) -m pytest

test-cov:
	@echo "🧪 Запуск тестів з покриттям..."
	$(PYTHON) -m pytest --cov=app --cov-report=html

clean:
	@echo "🧹 Очищення..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	@echo "✅ Очищено!"

run:
	@echo "🚀 Запуск бота..."
	$(PYTHON) -m app

docker-build:
	@echo "🐳 Збірка Docker образу..."
	docker-compose build

docker-up:
	@echo "🐳 Запуск Docker контейнерів..."
	docker-compose up -d

docker-down:
	@echo "🐳 Зупинка Docker контейнерів..."
	docker-compose down

docker-logs:
	@echo "📋 Логи Docker..."
	docker-compose logs -f bot

docker-restart:
	@echo "🔄 Перезапуск Docker контейнерів..."
	docker-compose restart

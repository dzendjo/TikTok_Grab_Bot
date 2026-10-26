# 🔥 Режим Розробки з Hot-Reload

Цей гайд показує як налаштувати автоматичне перезавантаження бота при змінах коду.

## 🚀 Швидкий Старт

### Варіант 1: Локальна Розробка (Рекомендовано)

```bash
# 1. Встановити залежності для розробки
pip install -r requirements-dev.txt

# 2. Запустити в режимі hot-reload
python dev.py
```

**Тепер кожна зміна в `app/` автоматично перезапускає бота!** 🎉

### Варіант 2: Docker з Hot-Reload

```bash
# Запустити dev окруження
docker-compose -f docker-compose.dev.yml up

# Зміни в app/ автоматично застосовуються
```

---

## 📋 Детальний Опис

### 1. Локальна Розробка з `watchfiles`

#### Встановлення:
```bash
pip install -r requirements-dev.txt
```

Що встановлюється:
- `watchfiles` - автоматичний перезапуск
- `black` - форматування коду
- `ruff` - швидкий лінтер
- `mypy` - перевірка типів
- `pytest` - тестування

#### Запуск:
```bash
python dev.py
```

#### Що відбувається:
```
🔥 Режим розробки з hot-reload
📁 Відстежується: /path/to/app
💡 Збережіть файл для автоматичного перезапуску
🛑 Ctrl+C для зупинки
============================================================
```

#### Приклад роботи:
```bash
# Змінюєте app/handlers/user.py
# Зберігаєте (Ctrl+S)
# Bot автоматично перезапускається з новим кодом!
```

---

### 2. Docker Hot-Reload

#### Структура:
```yaml
# docker-compose.dev.yml
services:
  bot:
    volumes:
      - ./app:/opt/app:ro  # Монтуємо код
    command: ["watchfiles", "python -m app", "app/"]
```

#### Запуск:
```bash
# Збірка dev образу
docker-compose -f docker-compose.dev.yml build

# Запуск з hot-reload
docker-compose -f docker-compose.dev.yml up
```

#### Переваги Docker-режиму:
- ✅ Ізольоване середовище
- ✅ MongoDB вже запущена
- ✅ Однакове оточення як на продакшені
- ✅ Hot-reload працює

---

## 🛠️ Інструменти Розробки

### Форматування Коду:
```bash
# Форматувати всі файли
black app/

# Перевірити без змін
black --check app/
```

### Лінтинг:
```bash
# Швидка перевірка
ruff check app/

# Виправити автоматично
ruff check --fix app/
```

### Перевірка Типів:
```bash
mypy app/
```

### Тестування:
```bash
# Запустити тести
pytest

# З покриттям
pytest --cov=app
```

---

## 📊 Порівняння Варіантів

| Критерій | Локальна Розробка | Docker Dev |
|----------|-------------------|------------|
| **Швидкість** | ⚡⚡⚡ Найшвидший | ⚡⚡ Швидкий |
| **Налаштування** | Проста | Потребує Docker |
| **Ізоляція** | ❌ Локальна система | ✅ Контейнер |
| **MongoDB** | Потрібна окремо | ✅ Включена |
| **Hot-Reload** | ✅ Дуже швидкий | ✅ Швидкий |

---

## 🎯 Рекомендації

### Для Початківців:
```bash
# Простий і швидкий старт
pip install -r requirements-dev.txt
python dev.py
```

### Для Production-Like:
```bash
# Максимально близько до продакшену
docker-compose -f docker-compose.dev.yml up
```

---

## 🔍 Troubleshooting

### Бот не перезапускається при змінах

**Локальна розробка:**
```bash
# Перевірте що watchfiles встановлено
pip show watchfiles

# Переустановіть
pip install --upgrade watchfiles
```

**Docker:**
```bash
# Перевірте що volume змонтовано
docker-compose -f docker-compose.dev.yml exec bot ls -la /opt/app

# Перебудуйте контейнер
docker-compose -f docker-compose.dev.yml build --no-cache
```

### Занадто часті перезапуски

Watchfiles відстежує всі файли. Виключіть непотрібні:

**dev.py:**
```python
run_process(
    watch_path,
    target=f"{sys.executable}",
    args=["-m", "app"],
    ignore_paths=["__pycache__", "*.pyc", ".git"],  # Додати
)
```

### Import помилки

```bash
# Переконайтесь що в правильному venv
which python

# Переустановіть залежності
pip install -r requirements.txt
```

#### Помилка: `ModuleNotFoundError: No module named 'PIL'`

Ця помилка виникає, коли бібліотека `Pillow` не встановлена або некоректно скомпільована (особливо на Python 3.14).

**Рішення:**
1. Переконайтесь, що `Pillow` є в `requirements.txt`.
2. Встановіть системні залежності (якщо ви на Linux):
   ```bash
   sudo apt-get install python3-dev libjpeg-dev zlib1g-dev gcc
   ```
3. Перевстановіть бібліотеку:
   ```bash
   pip uninstall Pillow
   pip install Pillow
   ```
4. Якщо ви використовуєте Docker — обов'язково перезберіть образ:
   ```bash
   docker-compose build --no-cache
   ```

---

## 💡 Поради

### 1. Швидке Налагодження
```python
# Додайте в код для дебагу
from app.utils import get_logger
logger = get_logger(__name__)

logger.debug(f"User data: {user}")
```

### 2. Логи в Реальному Часі
```bash
# В .env встановіть
LOG_LEVEL=DEBUG

# Запустіть dev режим
python dev.py
```

### 3. Тестування Змін
```bash
# 1. Змініть код
# 2. Збережіть файл (Ctrl+S)
# 3. Надішліть команду в бота
# 4. Бачте результат миттєво!
```

### 4. Використання з VSCode

**.vscode/launch.json:**
```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Bot Dev Mode",
      "type": "python",
      "request": "launch",
      "program": "${workspaceFolder}/dev.py",
      "console": "integratedTerminal"
    }
  ]
}
```

Натисніть **F5** для запуску з hot-reload!

---

## 🎬 Workflow Розробки

```bash
# 1. Запустити dev режим
python dev.py

# 2. Відкрити код в редакторі
code app/handlers/user.py

# 3. Внести зміни

# 4. Зберегти (Ctrl+S)
#    → Бот автоматично перезапускається

# 5. Протестувати в Telegram

# 6. Повторити кроки 3-5

# 7. Коміт коли готово
git add .
git commit -m "Add new feature"
```

---

## 📚 Додаткові Ресурси

- [watchfiles docs](https://watchfiles.helpmanual.io/)
- [aiogram docs](https://docs.aiogram.dev/)
- [Python asyncio](https://docs.python.org/3/library/asyncio.html)

---

**Розробка має бути швидкою та приємною!** 🚀

---

## 🚀 Makefile (Швидкі Команди)

Для зручності додано Makefile з популярними командами:

### Показати всі команди:
```bash
make help
```

### Розробка:
```bash
make dev              # Запустити з hot-reload
make dev-docker       # Docker з hot-reload
```

### Встановлення:
```bash
make install          # Основні залежності
make install-dev      # Dev залежності
```

### Якість коду:
```bash
make format           # Форматувати (black)
make lint             # Перевірити (ruff)
make lint-fix         # Виправити автоматично
make typecheck        # Перевірити типи (mypy)
make test             # Тести
make test-cov         # Тести з покриттям
```

### Docker:
```bash
make docker-build     # Збудувати образ
make docker-up        # Запустити
make docker-down      # Зупинити
make docker-logs      # Показати логи
make docker-restart   # Перезапустити
```

### Очищення:
```bash
make clean            # Очистити кеш та тимчасові файли
```

---

## ⚡ Найшвидший Старт

```bash
# Одна команда для початку:
make install-dev && make dev
```

Готово! Тепер розробка максимально швидка та зручна! 🎉

# Changelog

## [3.0.0] - 2025-12-18

### 🎉 Major Update: Python 3.13+ & Beanie 2.0

### Added
- Python 3.13+ support with modern features
- PEP 695 Generic Type Parameters (`def split_list[T](...)`)
- `.python-version` file for pyenv/asdf
- Enhanced README with v3.0 features
- Docker Python 3.14-slim base image

### Changed
- **Python**: Minimum version 3.13+ (was 3.11+)
- **Dockerfile**: Updated to Python 3.14-slim
- **aiogram**: 3.20.0 → 3.23.0
- **aiohttp**: 3.11.18 → 3.13.2
- **beanie**: 1.29.0 → 2.0.1 (BREAKING)
- **pymongo**: 4.12.1 → 4.15.5
- **motor**: 3.7.0 → 3.7.1
- **pydantic**: 2.11.4 → 2.12.5
- **pydantic-settings**: 2.7.1 → 2.12.0
- **redis**: 5.2.1 → 7.1.0

### Migration to Beanie 2.0
- Replaced `AsyncIOMotorClient` with `AsyncMongoClient`
- Updated `get_motor_collection()` to `get_pymongo_collection()`
- Changed from Motor to PyMongo Async API (future-proof)

### Code Modernization
- Replaced `typing.Dict/List/Callable/Awaitable` with built-in types
- Used `collections.abc.Callable` and `collections.abc.Awaitable`
- Applied Python 3.13+ generic type parameters syntax
- Removed legacy `TypeVar` usage

### Files Modified
- `requirements.txt` - All dependencies updated
- `Dockerfile` - Python 3.14-slim
- `app/database/__init__.py` - AsyncMongoClient
- `app/services/broadcast.py` - get_pymongo_collection()
- `app/draw.py` - get_pymongo_collection()
- `app/utils/helpers.py` - PEP 695 generics
- `app/middlewares/*.py` - collections.abc imports
- `README.md` - Complete rewrite for v3.0

### Notes
- ⚠️ Motor will be deprecated in May 2026
- ✅ This update prepares the codebase for the future
- ✅ All code is Python 3.13+ compatible
- ✅ Syntax verified with Python 3.13.7

## [2.0.0] - Previous version
- Initial modern template with aiogram 3.x

"""Localization loader using Jinja2 templates."""
from pathlib import Path
from typing import Dict

from jinja2 import Environment, DictLoader
from ruamel.yaml import YAML

yaml = YAML()


def flatten_dict(data: dict, parent_key: str = '') -> dict:
    """Flatten nested dictionary into dot-notation keys.

    Args:
        data: Dictionary to flatten
        parent_key: Parent key for nested items

    Returns:
        Flattened dictionary
    """
    items = {}
    for key, value in data.items():
        new_key = f"{parent_key}/{key}" if parent_key else key
        if isinstance(value, dict):
            items.update(flatten_dict(value, new_key))
        else:
            items[new_key] = value
    return items


def load_jinja_env_from_yaml(yaml_path: Path) -> Environment:
    """Load Jinja2 environment from YAML file.

    Args:
        yaml_path: Path to YAML file

    Returns:
        Jinja2 Environment with loaded templates
    """
    with open(yaml_path, 'r', encoding='utf-8') as file:
        data = yaml.load(file)

    flat_data = flatten_dict(data)
    return Environment(loader=DictLoader(flat_data))


class LocalizationManager:
    """Manager for localization templates."""

    def __init__(self, locales_dir: Path | None = None):
        """Initialize localization manager.

        Args:
            locales_dir: Directory with locale YAML files
        """
        if locales_dir is None:
            locales_dir = Path(__file__).parent

        self.locales: Dict[str, dict] = {}
        self._load_locales(locales_dir)

    def _load_locales(self, locales_dir: Path) -> None:
        """Load all locale files from directory.

        Args:
            locales_dir: Directory with locale YAML files
        """
        # Define display names for languages
        display_names = {
            'uk': '🇺🇦 UA',
            'en': '🇺🇸 EN',
            'ru': '🇷🇺 RU',
        }

        # Load all .yaml files from locales directory
        for yaml_path in locales_dir.glob('*.yaml'):
            lang_code = yaml_path.stem
            self.locales[lang_code] = {
                'text': display_names.get(lang_code, lang_code.upper()),
                'j2': load_jinja_env_from_yaml(yaml_path)
            }

    def get_environment(self, language: str) -> Environment:
        """Get Jinja2 environment for language.

        Args:
            language: Language code (uk, en, ru)

        Returns:
            Jinja2 Environment
        """
        return self.locales.get(language, self.locales['en'])['j2']

    def get_text(self, language: str, template_path: str, **kwargs) -> str:
        """Get rendered text for language.

        Args:
            language: Language code
            template_path: Template path (e.g., 'start/mt')
            **kwargs: Template variables

        Returns:
            Rendered text
        """
        env = self.get_environment(language)
        template = env.get_template(template_path)
        return template.render(**kwargs)

    @property
    def available_languages(self) -> dict:
        """Get available languages with their display text.

        Returns:
            Dictionary of language codes and display texts
        """
        return {code: data['text'] for code, data in self.locales.items()}


# Singleton instance
localization = LocalizationManager()

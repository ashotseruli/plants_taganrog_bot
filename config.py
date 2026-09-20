"""Загрузка и валидация config.yaml.

Модуль намеренно не логирует на уровне import — только внутри функций.
Так порядок импорта относительно logging.basicConfig() в app.py не важен.

CONFIG_PATH читается из env (CONFIG_PATH) или берётся рядом с этим файлом.
"""
import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = os.getenv("CONFIG_PATH", str(BASE_DIR / "config.yaml"))


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_config(cfg: dict) -> None:
    """Проверяет, что в конфиге есть все критичные ключи и они валидны.

    Проверяются и наличие, и (там где важно) типы/значения — иначе ошибку в yaml
    придётся искать уже во время работы бота."""
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml: корень должен быть словарём")
    for section in ("phrases", "placeholders", "prompts", "reactions", "llm", "new_member"):
        if section not in cfg:
            raise ValueError(f"config.yaml: отсутствует секция '{section}'")

    if not isinstance(cfg["phrases"], list) or not cfg["phrases"]:
        raise ValueError("config.yaml: 'phrases' должен быть непустым списком")
    if not all(isinstance(p, str) for p in cfg["phrases"]):
        raise ValueError("config.yaml: все элементы 'phrases' должны быть строками")

    if not isinstance(cfg["placeholders"], list) or not cfg["placeholders"]:
        raise ValueError("config.yaml: 'placeholders' должен быть непустым списком")
    if not all(isinstance(p, str) for p in cfg["placeholders"]):
        raise ValueError("config.yaml: все элементы 'placeholders' должны быть строками")

    r = cfg["reactions"]
    if not isinstance(r, dict):
        raise ValueError("config.yaml: 'reactions' должен быть словарём")
    pos = r.get("positive") or []
    neg = r.get("negative") or []
    if not isinstance(pos, list) or not isinstance(neg, list):
        raise ValueError("config.yaml: 'reactions.positive'/'negative' должны быть списками")
    if not pos and not neg:
        raise ValueError("config.yaml: нужна хотя бы одна реакция (positive/negative)")

    if not isinstance(cfg["prompts"], dict):
        raise ValueError("config.yaml: 'prompts' должен быть словарём")
    if not isinstance(cfg["prompts"].get("system"), str) or not cfg["prompts"]["system"].strip():
        raise ValueError("config.yaml: 'prompts.system' отсутствует, не строка или пуст")

    llm = cfg["llm"]
    if not isinstance(llm, dict):
        raise ValueError("config.yaml: 'llm' должен быть словарём")
    num_keys = (
        "thinking_delay", "hard_timeout", "max_words", "max_input_chars",
        "history_max_messages", "history_max_chars",
        "num_ctx", "num_predict", "temperature", "top_p", "repeat_penalty",
    )
    for key in num_keys:
        if key not in llm:
            raise ValueError(f"config.yaml: 'llm.{key}' отсутствует")
        if not isinstance(llm[key], (int, float)):
            raise ValueError(f"config.yaml: 'llm.{key}' должен быть числом")
    if llm.get("user_cooldown") is not None and not isinstance(llm["user_cooldown"], (int, float)):
        raise ValueError("config.yaml: 'llm.user_cooldown' должен быть числом")

    nm = cfg["new_member"]
    if not isinstance(nm, dict):
        raise ValueError("config.yaml: 'new_member' должен быть словарём")
    for key in ("kick_after_seconds", "max_message_age_seconds"):
        if key not in nm:
            raise ValueError(f"config.yaml: 'new_member.{key}' отсутствует")
        if not isinstance(nm[key], (int, float)) or nm[key] < 0:
            raise ValueError(f"config.yaml: 'new_member.{key}' должен быть числом ≥ 0")
    if nm["kick_after_seconds"] <= 0:
        raise ValueError("config.yaml: 'new_member.kick_after_seconds' должен быть > 0")

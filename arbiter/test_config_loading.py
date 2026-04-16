from pathlib import Path

from arbiter.config import settings


def test_project_dotenv_prefers_repo_root(monkeypatch, tmp_path):
    repo_root = tmp_path / "arbiter-repo"
    package_dir = repo_root / "arbiter" / "config"
    package_dir.mkdir(parents=True)

    repo_env = repo_root / ".env"
    repo_env.write_text("KALSHI_API_KEY_ID=test-key\n", encoding="utf-8")
    package_env = repo_root / "arbiter" / ".env"
    package_env.write_text("KALSHI_API_KEY_ID=wrong-key\n", encoding="utf-8")

    loaded = []

    monkeypatch.setattr(settings, "load_dotenv", lambda path, override=True: loaded.append(Path(path)))

    result = settings._load_project_dotenv(package_dir / "settings.py")

    assert result == repo_env
    assert loaded == [repo_env]


def test_project_dotenv_falls_back_to_package_root(monkeypatch, tmp_path):
    repo_root = tmp_path / "arbiter-repo"
    package_dir = repo_root / "arbiter" / "config"
    package_dir.mkdir(parents=True)

    package_env = repo_root / "arbiter" / ".env"
    package_env.write_text("KALSHI_API_KEY_ID=test-key\n", encoding="utf-8")

    loaded = []

    monkeypatch.setattr(settings, "load_dotenv", lambda path, override=True: loaded.append(Path(path)))

    result = settings._load_project_dotenv(package_dir / "settings.py")

    assert result == package_env
    assert loaded == [package_env]


def test_load_config_resolves_kalshi_key_relative_to_loaded_dotenv(monkeypatch, tmp_path):
    repo_root = tmp_path / "arbiter-repo"
    package_dir = repo_root / "arbiter" / "config"
    package_dir.mkdir(parents=True)

    monkeypatch.setattr(settings, "_DOTENV_PATH", repo_root / ".env")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "./keys/kalshi_private.pem")

    cfg = settings.load_config()

    assert cfg.kalshi.private_key_path == str((repo_root / "keys" / "kalshi_private.pem").resolve())

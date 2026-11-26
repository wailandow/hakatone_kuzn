from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore', env_ignore_empty=True)
    db_path: Path = Path('data/app.db')
    public_base_url: str = 'http://127.0.0.1:8000'
    webim_base_url: str = 'http://127.0.0.1:8000/mock-webim'
    webim_bot_token: SecretStr | None = None
    webim_webhook_token: SecretStr | None = None
    manager_token: SecretStr | None = None
    llm_base_url: str = 'https://api.openai.com/v1'
    llm_api_key: SecretStr | None = None
    llm_model: str = ''
    llm_fast_model: str = ''
    llm_proxy: SecretStr | None = None
    llm_price_in_per_1m: float = Field(0, ge=0)
    llm_price_out_per_1m: float = Field(0, ge=0)
    usd_to_rub: float = Field(0, ge=0)
    font_dir: Path = Path('/usr/share/fonts/truetype/dejavu')

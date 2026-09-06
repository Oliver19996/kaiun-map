from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    aisstream_api_key: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_max_tokens: int = 400
    daily_ai_request_limit: int = 200
    ai_requests_per_minute: int = 10
    session_secret: str = "dev-change-SESSION_SECRET"


settings = Settings()

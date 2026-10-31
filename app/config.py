"""Runtime configuration. Everything comes from the environment (or .env)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:  # optional
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=False)
except Exception:  # pragma: no cover
    pass


def env(name: str, default: str = '') -> str:
    """VAADA_* settings; the old LANE_* names still work so existing .env files keep running."""
    v = os.getenv(name)
    if v is None and name.startswith('VAADA_'):
        v = os.getenv('LANE_' + name[6:])
    return default if v is None else v


def flag(name: str, default: bool = False) -> bool:
    return env(name, 'true' if default else 'false').strip().lower() in {'1', 'true', 'yes', 'on'}


DATABASE_URL = env('DATABASE_URL', f"sqlite:///{ROOT / 'vaada.db'}")
SECRET_KEY = env('VAADA_SECRET_KEY', '')            # signs sessions + encrypts stored secrets
PUBLIC_URL = env('VAADA_PUBLIC_URL', 'http://localhost:8000').rstrip('/')
DEMO_MODE = flag('VAADA_DEMO_MODE', True)           # enables sandbox agency + simulator
INLINE_WORKER = flag('VAADA_INLINE_WORKER', True)   # run worker thread inside web process
BINDING_EPSILON_S = int(env('VAADA_BINDING_EPSILON_S', '90'))  # clock-skew window for boundary binding
LABELER = env('VAADA_LABELER', 'rules')             # rules | openai | anthropic
EXTRACTOR = env('VAADA_EXTRACTOR', 'rules')         # rules | openai | anthropic (LLM output is still guarded)
EXTRACT_MODE = env('VAADA_EXTRACT_MODE', 'vote')    # vote: rules + model read every call | fallback: model only when rules abstain
OPENAI_API_KEY = env('OPENAI_API_KEY')
OPENAI_MODEL = env('VAADA_OPENAI_MODEL', 'gpt-4o-mini')
OPENAI_BASE = env('VAADA_OPENAI_BASE', 'https://api.openai.com/v1')
GROQ_API_KEY = env('GROQ_API_KEY')
GROQ_MODEL = env('VAADA_GROQ_MODEL', 'llama-3.3-70b-versatile')
GROQ_BASE = env('VAADA_GROQ_BASE', 'https://api.groq.com/openai/v1')
MODEL_PROVIDERS = ('openai', 'groq', 'anthropic')


def chat_api(provider: str):
    """(url, key, model) for OpenAI-compatible providers; None when not configured."""
    if provider == 'openai' and OPENAI_API_KEY:
        return OPENAI_BASE.rstrip('/') + '/chat/completions', OPENAI_API_KEY, OPENAI_MODEL
    if provider == 'groq' and GROQ_API_KEY:
        return GROQ_BASE.rstrip('/') + '/chat/completions', GROQ_API_KEY, GROQ_MODEL
    return None
ANTHROPIC_API_KEY = env('ANTHROPIC_API_KEY')
ANTHROPIC_MODEL = env('VAADA_ANTHROPIC_MODEL', 'claude-haiku-5-5')
VAPI_BASE = env('VAPI_BASE', 'https://api.vapi.ai')
HIGHLEVEL_VERSION = env('HIGHLEVEL_VERSION', '2021-04-15')
HIGHLEVEL_BASE = env('HIGHLEVEL_BASE', 'https://services.leadconnectorhq.com')
RETELL_BASE = env('RETELL_BASE', 'https://api.retellai.com')
SESSION_HTTPS_ONLY = flag('VAADA_SESSION_HTTPS_ONLY', False)


def secret_key() -> str:
    if SECRET_KEY:
        return SECRET_KEY
    # Dev fallback: stable per checkout, never use in production.
    p = ROOT / '.vaada_dev_secret'
    if not p.exists():
        import secrets
        p.write_text(secrets.token_urlsafe(48))
    return p.read_text().strip()

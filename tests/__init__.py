import warnings

# In-memory SQLite stores are left to the garbage collector in tests.
warnings.filterwarnings("ignore", category=ResourceWarning)

# Keep password hashing fast in tests (production default stays 600k).
from lfmshim import store as _store  # noqa: E402

_store.PBKDF2_ITERATIONS = 1_000

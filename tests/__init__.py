import warnings

# In-memory SQLite stores are left to the garbage collector in tests.
warnings.filterwarnings("ignore", category=ResourceWarning)

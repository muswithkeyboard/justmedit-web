def __getattr__(name: str):
    if name == "__version__":
        from .constants import ENGINE_VERSION

        return ENGINE_VERSION
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

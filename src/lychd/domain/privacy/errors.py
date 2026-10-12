"""Safe privacy-boundary refusal without raw source or identity values."""


class PrivacyRefused(ValueError):  # noqa: N818 - stable domain refusal
    """A refusal code that never interpolates source or candidate values."""

    def __init__(self, code: str) -> None:
        """Retain only a stable safe refusal code."""
        self.code = code
        super().__init__(f"Privacy preparation refused: {code}.")

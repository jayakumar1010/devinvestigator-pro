class GitHubAPIError(Exception):
    """A GitHub API call failed. Messages never include credentials or signed URLs."""

    def __init__(self, status_code: int | None, path: str, message: str) -> None:
        super().__init__(f"GitHub API {status_code or 'error'} for {path}: {message}")
        self.status_code = status_code
        self.path = path

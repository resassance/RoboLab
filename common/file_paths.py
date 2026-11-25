DEFAULT_UPLOAD_BYTES = 32 * 1024 * 1024
UPLOAD_PREFIX = ".robolab-upload-"


def valid_name(name: str) -> bool:
    return (
        name.isprintable()
        and name not in {".", ".."}
        and not name.startswith(UPLOAD_PREFIX)
        and "/" not in name
        and "\\" not in name
        and len(name.encode("utf-8")) <= 255
    )

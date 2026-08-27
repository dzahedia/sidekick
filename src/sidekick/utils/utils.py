from pathlib import Path


def expand_file_patterns(root: str, entries: list[str]) -> list[str]:
    root_path = Path(root).resolve()
    files = []

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue

        if "*" in entry:
            # Important: glob relative to the root, not arbitrary absolute paths
            for match in root_path.glob(entry):
                resolved = match.resolve()

                # Prevent symlink / traversal escapes
                if not resolved.is_relative_to(root_path):
                    raise ValueError(f"Path escapes root: {entry}")

                if resolved.is_file():
                    files.append(resolved.relative_to(root_path).as_posix())
        else:
            files.append(entry)

    # Remove duplicates while preserving order
    return list(dict.fromkeys(files))
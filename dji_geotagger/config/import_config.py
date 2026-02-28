from pathlib import Path
from dji_geotagger.config.default_ppk_dict import DEFAULT_PPK_CONF


def import_rtklib_config(user_override: dict = None, output_path: Path = None) -> Path:
    """
    Merge default config with user override, export to .conf file

    Returns:
        Path to generated .conf file
    """
    # Handle default output path
    if output_path is None:
        root_path = Path(__file__).resolve().parent.parent
        output_path = root_path / "config" / "rtklib_auto.conf"

    config = DEFAULT_PPK_CONF.copy()
    if user_override:
        config.update(user_override)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        for key, val in config.items():
            f.write(f"{key}={val}\n")

    return output_path
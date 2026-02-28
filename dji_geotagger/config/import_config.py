from pathlib import Path
from dji_geotagger.config.default_ppk_dict import DEFAULT_PPK_CONF


def override_rtklib_config(
        user_conf: dict = None, 
        output_path: Path = None) -> Path:
    """
    Merge default config with user override, export to .conf file

    Returns:
        Path to generated .conf file
    """
    # Handle default output path
    if output_path is None:
        output_path = Path.cwd() / "DGT_output" / "rtklib_config" / "rtklib_auto.conf"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    config = DEFAULT_PPK_CONF.copy()
    if user_conf:
        config.update(user_conf)


    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        for key, val in config.items():
            f.write(f"{key}={val}\n")

    return output_path
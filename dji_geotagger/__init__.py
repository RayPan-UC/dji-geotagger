from dji_geotagger.tools.logging_setup import configure_logging

# Configure console logging on import so existing scripts keep printing exactly
# what they printed before. Callers that want the output elsewhere - a GUI, a
# log file - either attach their own handler to the "dji_geotagger" logger or
# call configure_logging(console=False) to silence this one.
configure_logging()

from dji_geotagger.ppk.raw_converter import raw2rinex
from dji_geotagger.ppk.ppk_solver import process_ppk, sum_file_parser
from dji_geotagger.ppk.base_position import (
    resolve_base_position,
    build_base_position,
)
from dji_geotagger.ppk.ppp_service import run_online_ppp, PPPServiceError
from dji_geotagger.tools.progress import (
    Progress,
    ProgressEvent,
    OperationCancelled,
)
from dji_geotagger.tools.install_RTKLIB import ensure_rtklib
from dji_geotagger.core.pos_parser import pos2df
from dji_geotagger.core.mrk_parser import mrk2df
from dji_geotagger.core.xml_parser import parse_img_dir
from dji_geotagger.core.camera_pos_solver import compute_camera_position
from dji_geotagger.core.geotag import geotag
from dji_geotagger.core.transform import (
    transform_coordinates,
    resolve_source_crs,
    make_utm_crs,
    rebase_projected_crs,
    TransformError,
)
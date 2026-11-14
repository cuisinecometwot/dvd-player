"""Text shown in the rip dialog's album form, shared by the GTK and Qt dialogs."""

from domain.cdcache import COVER_USER, SOURCE_CD_TEXT, SOURCE_EDITED, SOURCE_MUSICBRAINZ
from dvdplayer.cdinfo import CdInfo


def cover_text(cd_info: CdInfo, error: str) -> str:
    """Subtitle of the cover row."""
    if error:
        return error
    if cd_info.cover_path is None:
        return "None found — choose an image" if not cd_info.resolving else "Looking…"
    if cd_info.cover_source == COVER_USER:
        return "Your image · embedded in each FLAC and saved as cover.jpg/png"
    return "From the Cover Art Archive · embedded in each FLAC and saved as cover.jpg"


def album_status_text(cd_info: CdInfo) -> str:
    """Where the names in the form came from."""
    if cd_info.resolving:
        text = "Looking up the disc (CD-Text, then MusicBrainz)…"
    else:
        text = {
            SOURCE_EDITED: "Your earlier edits for this disc. Change anything before ripping.",
            SOURCE_CD_TEXT: "From the disc's own CD-Text. Edit anything before ripping.",
            SOURCE_MUSICBRAINZ: "Found on MusicBrainz. Edit anything before ripping.",
        }.get(cd_info.source, "Not found on the disc or online: type the details in.")
    return text + " Edits are remembered for this disc."

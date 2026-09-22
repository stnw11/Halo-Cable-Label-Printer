"""Print backends.

Which one is correct depends on what the Brady driver actually accepts,
which is spec open item 1 and unresolved until someone runs the spike on
the real machine. The interface exists so that answer changes one class
rather than the shape of the agent.

Start on `null`. It validates and logs and prints nothing, which is how the
whole pipeline gets proven correct before any media is loaded.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows forbids these in a filename; control characters too.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_JOB_NAME = 100


def job_name(sidecar: dict, fallback: Path) -> str:
    """What the printer should call this job, safe to use as a filename.

    The Wraptor holds jobs in a stored-file list that someone picks from at
    the printer, so this is read by a human: 'Blue CAT6 -
    BL-0100...BL-0102'. The Docker half puts it in the sidecar; an older
    sidecar without one falls back to the payload's own name.
    """
    raw = str(sidecar.get("job_name") or "").strip()
    cleaned = _UNSAFE.sub("", raw).strip(" .")
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_JOB_NAME].strip() or fallback.stem


class PrintError(RuntimeError):
    """The document was not handed to the print subsystem.

    The message ends up in the result sidecar and then in the Docker half's
    log and the Halo status field, so it must read usefully to someone who
    is not looking at this machine.
    """


class PrintBackend:
    name = "base"

    def __init__(self, printer_name: str, timeout_seconds: int = 120, **kwargs):
        self.printer_name = printer_name
        self.timeout_seconds = timeout_seconds
        self.options = kwargs

    def preflight(self) -> None:
        """Raise PrintError if this backend cannot work on this machine.
        Called at startup so a misconfigured agent fails before a job
        arrives, not on the first real print."""

    def print(self, payload_path: Path, sidecar: dict) -> None:
        raise NotImplementedError


class NullBackend(PrintBackend):
    """Validates and logs; prints nothing. Consumes no media.

    Every acceptance run should start here (spec criterion 23). It is also
    the right backend for a soak test, and for reproducing a queue problem
    without burning labels.
    """

    name = "null"

    def print(self, payload_path: Path, sidecar: dict) -> None:
        if not payload_path.exists():
            raise PrintError(f"payload missing: {payload_path}")
        size = payload_path.stat().st_size
        logger.info(
            "NULL backend: would print %d label(s) for %s %s..%s as %r (%s, %d bytes) to %r",
            sidecar["label_count"],
            sidecar["prefix"],
            sidecar["first_number"],
            sidecar["last_number"],
            job_name(sidecar, payload_path),
            payload_path.name,
            size,
            self.printer_name,
        )


def page_boxes(image_size: tuple[int, int], page_count: int) -> list[tuple[int, int, int, int]]:
    """Crop boxes for each label page in a stacked strip.

    The Docker half renders one tall image, label pages top to bottom, so a
    job stays one file and one sidecar. The sidecar's label_count says how
    many pages it holds. Pure arithmetic, kept out of the printing code so
    it can be tested anywhere.
    """
    width, height = image_size
    if page_count < 1:
        raise PrintError(f"a job cannot have {page_count} pages")
    if height % page_count:
        raise PrintError(
            f"the payload is {height}px tall, which does not divide into {page_count} "
            f"label pages. The sidecar and the image disagree; refusing to guess."
        )
    page_height = height // page_count
    return [(0, i * page_height, width, (i + 1) * page_height) for i in range(page_count)]


class GdiBackend(PrintBackend):
    """Raster printing straight through the Windows driver.

    Needs nothing on the print host but the printer's own driver: no PDF
    reader, no Brady software. The Docker half renders the labels at the
    media's dpi and this draws each page onto the printer's device context
    at its exact physical size, so "no scaling" is arithmetic rather than a
    setting someone can get wrong in a print dialog.

    The document name is set here, which is what the Wraptor lists in the
    stored-job menu people pick from.
    """

    name = "gdi"

    def preflight(self) -> None:
        self._imports()

    @staticmethod
    def _imports():
        try:
            import win32con  # noqa: F401
            import win32ui  # noqa: F401
            from PIL import Image, ImageWin  # noqa: F401
        except ImportError as exc:
            raise PrintError(
                f"the gdi backend needs pywin32 and pillow on this machine ({exc}). "
                f"Install them with: python -m pip install -r requirements.txt"
            ) from exc
        return win32con, win32ui, Image, ImageWin

    def print(self, payload_path: Path, sidecar: dict) -> None:
        if sidecar.get("render_format") != "png":
            raise PrintError(
                f"the gdi backend prints the rendered image; this job is "
                f"{sidecar.get('render_format')!r}. Set RENDER_FORMAT=png on the Docker side."
            )
        win32con, win32ui, Image, ImageWin = self._imports()

        with Image.open(payload_path) as strip:
            strip.load()
            pages = page_boxes(strip.size, int(sidecar["label_count"]))
            name = job_name(sidecar, payload_path)

            dc = win32ui.CreateDC()
            try:
                dc.CreatePrinterDC(self.printer_name)
            except Exception as exc:
                raise PrintError(
                    f"could not open printer {self.printer_name!r}: {exc}. The service "
                    f"account must be able to see it; the agent lists what it can see at startup."
                ) from exc

            # Device pixels per inch, which need not match the image's dpi:
            # the page is drawn to its physical size either way.
            dpi_x = dc.GetDeviceCaps(win32con.LOGPIXELSX)
            dpi_y = dc.GetDeviceCaps(win32con.LOGPIXELSY)
            width_dev = int(round(float(sidecar["page_width_in"]) * dpi_x))
            height_dev = int(round(float(sidecar["page_height_in"]) * dpi_y))
            logger.info(
                "printing %d page(s) of %s as %r at %dx%d device px (%d dpi)",
                len(pages), payload_path.name, name, width_dev, height_dev, dpi_x,
            )

            try:
                dc.StartDoc(name)
                try:
                    for box in pages:
                        dc.StartPage()
                        page = strip.crop(box)
                        ImageWin.Dib(page).draw(dc.GetHandleOutput(), (0, 0, width_dev, height_dev))
                        dc.EndPage()
                    dc.EndDoc()
                except Exception:
                    dc.AbortDoc()
                    raise
            except PrintError:
                raise
            except Exception as exc:
                raise PrintError(f"the driver rejected the job: {exc}") from exc
            finally:
                dc.DeleteDC()


class BradyBackend(PrintBackend):
    """Brady Workstation's own automation entry point.

    Most likely to handle wrap geometry and applicator sequencing correctly,
    and may well become the default. Unimplemented until the spike says what
    the entry point actually is.
    """

    name = "brady"

    def preflight(self) -> None:
        raise PrintError(
            "the brady backend is not implemented yet. Run the spike from build order "
            "step 2 on the print host, write the answer into the spec, then implement "
            "this class."
        )

    def print(self, payload_path: Path, sidecar: dict) -> None:
        self.preflight()


BACKENDS = {
    cls.name: cls for cls in (NullBackend, GdiBackend, BradyBackend)
}


def build_backend(name: str, printer_name: str, timeout_seconds: int = 120, **kwargs) -> PrintBackend:
    try:
        cls = BACKENDS[name]
    except KeyError:
        raise PrintError(
            f"unknown backend {name!r}. Available: {', '.join(sorted(BACKENDS))}"
        ) from None
    return cls(printer_name=printer_name, timeout_seconds=timeout_seconds, **kwargs)


def list_printers() -> list[str]:
    """Every printer this process can see.

    Printer drivers are per-user in ways that surprise people: a driver
    installed under an admin's interactive session can be invisible to the
    service account. Enumerating as the ACTUAL service account is the whole
    point, which is why this runs inside the agent rather than in an
    installer script.
    """
    try:
        import win32print  # type: ignore
    except ImportError:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [p[2] for p in win32print.EnumPrinters(flags)]


def running_on_windows() -> bool:
    return os.name == "nt"

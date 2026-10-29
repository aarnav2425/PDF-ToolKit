"""Runs pdf2docx in its own process, so a slow, huge or crashing conversion can't take the web app down."""
import sys


def patch_pymupdf():
    """Older pdf2docx calls Rect.get_area(), which some PyMuPDF releases dropped; add it back."""
    for modname in ("pymupdf", "fitz"):
        try:
            mod = __import__(modname)
        except ImportError:
            continue
        for cls in (getattr(mod, "Rect", None), getattr(mod, "IRect", None)):
            if cls is not None and not hasattr(cls, "get_area"):
                cls.get_area = lambda self, unit="px": abs(self.width * self.height)


def main(src, dst, max_pages):
    patch_pymupdf()
    try:
        from pdf2docx import Converter
        import pymupdf
    except ImportError:
        return 3                      # library missing
    with pymupdf.open(src) as d:
        if len(d) > max_pages:
            return 4                  # too many pages
    cv = Converter(src)
    try:
        cv.convert(dst)
    finally:
        cv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))

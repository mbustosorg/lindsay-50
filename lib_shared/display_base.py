"""Polymorphic display interface shared by the Pi and the browser preview.

The Pi's `MatrixDisplay` (hzeller rpi-rgb-led-matrix backed) and the browser's
`WebDisplay` (Pillow-backed) both subclass `DisplayBase`. They differ only in
how they pace a frame: the Pi calls `SwapOnVSync` to wait for the panel's next
vertical refresh; the browser's rAF loop in preview.js does the pacing, so
`WebDisplay.render` is a plain composite with no swap step.

The composite step (clear canvas, draw effect, draw scroller) is the same in
both subsystems — only the swap is different. The base class declares the
surface (clear, width, height, canvas) the effects and the coordinator rely
on; subclasses implement `render(effect, scroller)` to do the work.
"""


class DisplayBase:
    """Abstract display that owns a frame canvas and composites one frame.

    Subclasses must implement `render(effect, scroller)`. The composite
    sequence is "clear canvas, draw the active effect, draw the scroller,
    swap to the panel" — subclasses that have no swap step (the browser) just
    omit the final call.
    """

    width: int
    height: int
    canvas: object

    def clear(self):
        """Blank the panel immediately so no frame stays lit after we exit."""
        raise NotImplementedError

    def render(self, effect, scroller):
        """Composite one frame: clear, draw effect, draw scroller, swap.

        Subclasses implement this with their own swap step (or no swap, in
        the browser's case). The base class raises — the contract is that
        every display knows how to render its own frame.
        """
        raise NotImplementedError

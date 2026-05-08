import time
import displayio
import terminalio
import adafruit_logging as logging
from adafruit_display_text.label import Label

log = logging.getLogger("heart")


class Scroller:
    def __init__(self, matrix, color=0xFF0000, frame_delay=0.04, offset_seconds=1.0):
        self.display = matrix.display
        self.frame_delay = frame_delay
        self.offset_seconds = offset_seconds
        self.last_frame = 0.0
        self.start_time = 0.0
        self.text_width = 0
        self._color = color

        self.group = displayio.Group()
        self.top = Label(terminalio.FONT, text="", color=color)
        self.bottom = Label(terminalio.FONT, text="", color=color)
        # Centers of the two 64x32 panels.
        self.top.y = 16
        self.bottom.y = 48
        self.top.x = self.display.width
        self.bottom.x = self.display.width
        self.group.append(self.top)
        self.group.append(self.bottom)
        self.display.root_group = self.group

    def set_brightness(self, b):
        c = self._color
        r = int(((c >> 16) & 0xFF) * b)
        g = int(((c >> 8) & 0xFF) * b)
        bl = int((c & 0xFF) * b)
        scaled = (r << 16) | (g << 8) | bl
        self.top.color = scaled
        self.bottom.color = scaled

    def set_text(self, text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        text = str(text)
        log.debug("New scroll text: %r", text)
        self.top.text = text
        self.bottom.text = text
        self.text_width = self.top.bounding_box[2]
        self.top.x = self.display.width
        self.bottom.x = self.display.width
        now = time.monotonic()
        self.start_time = now
        self.last_frame = now

    def tick(self):
        if not self.top.text:
            return

        now = time.monotonic()
        elapsed = now - self.last_frame
        if elapsed < self.frame_delay:
            return

        # Move one pixel per frame_delay's worth of elapsed time, so a slow
        # main-loop iteration produces a multi-pixel catch-up rather than a
        # delayed single step.
        pixels = int(elapsed / self.frame_delay)
        self.last_frame += pixels * self.frame_delay

        end_x = -self.text_width
        width = self.display.width

        new_top = self.top.x - pixels
        if new_top < end_x:
            new_top = width
        self.top.x = new_top

        if now - self.start_time >= self.offset_seconds:
            new_bot = self.bottom.x - pixels
            if new_bot < end_x:
                new_bot = width
            self.bottom.x = new_bot
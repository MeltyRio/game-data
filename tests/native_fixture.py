"""Self-authored two-joint flag in RK/ANIM formats. No game bytes or artwork."""
import math
from pathlib import Path
import struct

import numpy as np
from PIL import Image


def write_fixture(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)

    def text(value):
        return value.encode().ljust(64, b"\0")

    # Native upright is negative Y; Rx(pi) in the GLB makes this flag Y-up.
    vertices = [(-.45, 0, 0, 0, 0), (.45, 0, 0, 1, 0),
                (-.45, -1, 0, 0, .5), (.45, -1, 0, 1, .5),
                (-.45, -2, 0, 0, 1), (.45, -2, 0, 1, 1)]
    bind = np.repeat(np.eye(4, dtype="<f4")[None], 2, axis=0)
    bind[1, 1, 3] = -1
    bones = b"".join(struct.pack("<3i", parent, index, 0) + bind[index].T.tobytes() + text(name)
                     for index, (parent, name) in enumerate([(-1, "root"), (0, "flag")]))
    parts = [
        (1, 1, struct.pack("<4I", 4, 0, 0, 0)),
        (2, 1, text("flag")),
        (3, 6, b"".join(struct.pack("<5f", *vertex) for vertex in vertices)),
        (4, 12, struct.pack("<12H", 0, 1, 2, 1, 3, 2, 2, 3, 4, 3, 5, 4)),
        (7, 2, bones), (13, 1, struct.pack("<H2B", 1026, 12, 0)),
        (16, 1, text("flag")),
        (17, 6, b"".join(struct.pack("<4B4H", joint, 0, 0, 0, 65535, 0, 0, 0)
                          for joint in [0, 0, 1, 1, 1, 1])),
    ]
    offset = 80 + 24 * 16
    entries, body = [], bytearray()
    for tag, count, data in parts:
        entries.append(struct.pack("<4I", tag, offset, count, len(data)))
        body.extend(data)
        offset += len(data)
    (folder / "flag.rk").write_bytes(struct.pack("<8sII64s", b"RKFORMAT", 5, 2, text("Synthetic flag"))
                                    + b"".join(entries).ljust(24 * 16, b"\0") + body)
    # Type 0 global poses, 46 frames / 15 fps; the child swings around its bind.
    frames = bytearray()
    for index in range(46):
        angle = math.sin(index * 2 * math.pi / 45) * .65
        frames.extend(struct.pack("<8f", 0, 0, 0, 1, 0, 0, 0, 1))
        frames.extend(struct.pack("<8f", 0, 0, math.sin(angle / 2), math.cos(angle / 2), 0, -1, 0, 1))
    (folder / "flag.anim").write_bytes(struct.pack("<8sII64sIII", b"RKFORMAT", 5, 2, text("flag"), 2, 46, 0) + frames)
    (folder / "flag.csv").write_text("rest,0,0,15\nwave,0,45,15\n", encoding="utf-8")
    (folder / "flag.xml").write_text("<MeshList/><AnimationList/>", encoding="utf-8")
    (folder / "flag.rkm").write_text("DiffuseTexture=flag\nShader=rkshader\nNoCompress=true\n"
                                     "ClampMode=RK_REPEAT\nBlendMode=none\nCull=0\nDepthWrite=1\nDepthTest=1\n", encoding="utf-8")
    image = Image.new("RGBA", (16, 16))
    image.putdata([(40, 180, 210, 255) if (x // 4 + y // 4) % 2 == 0 else (255, 205, 60, 255)
                   for y in range(16) for x in range(16)])
    image.save(folder / "flag.png")

"""
Reads the version information of an android apk

The version of an apk is stored in its manifest, which is an android binary xml file.
Only the parts of the format that are needed to read the version are parsed here.
"""

from dataclasses import dataclass
from pathlib import Path
import struct
import zipfile


# The chunk types of the android binary xml
RES_STRING_POOL_TYPE = 0x0001
RES_XML_START_ELEMENT_TYPE = 0x0102

# The types of the attribute values
TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_INT_BOOLEAN = 0x12

# The flag that marks the strings of a string pool as utf-8
UTF8_FLAG = 0x100


@dataclass
class ApkVersion:
    """The version of an apk"""

    version_code: int | None = None
    version_name: str | None = None

    def __str__(self) -> str:
        if self.version_name is None:
            return str(self.version_code)

        return f'{self.version_name} ({self.version_code})'


def parse_string_pool(data: bytes, offset: int) -> list[str]:
    """Reads the strings of a string pool chunk"""

    (
        chunk_type,
        header_size,
        _chunk_size,
        string_count,
        _style_count,
        flags,
        strings_start,
        _styles_start,
    ) = struct.unpack_from('<HHIIIIII', data, offset)

    if chunk_type != RES_STRING_POOL_TYPE:
        raise ValueError('Not a string pool chunk')

    utf8 = bool(flags & UTF8_FLAG)

    strings: list[str] = []

    for index in range(string_count):
        (string_offset,) = struct.unpack_from('<I', data, offset + header_size + index * 4)
        position = offset + strings_start + string_offset

        if utf8:
            # The length in utf-16 characters, then the length in bytes
            length = data[position]
            position += 1
            if length & 0x80:
                length = ((length & 0x7F) << 8) | data[position]
                position += 1

            byte_length = data[position]
            position += 1
            if byte_length & 0x80:
                byte_length = ((byte_length & 0x7F) << 8) | data[position]
                position += 1

            strings.append(data[position:position + byte_length].decode('utf-8', 'replace'))
        else:
            (length,) = struct.unpack_from('<H', data, position)
            position += 2
            if length & 0x8000:
                (length,) = struct.unpack_from('<H', data, position)
                position += 2

            strings.append(data[position:position + length * 2].decode('utf-16-le', 'replace'))

    return strings


def parse_manifest_attributes(data: bytes) -> dict[str, str | int | bool]:
    """Reads the attributes of the manifest element of an android binary xml file"""

    strings: list[str] = []

    # The header of the file is 8 bytes, the chunks follow it
    offset = 8

    while offset + 8 <= len(data):
        chunk_type, header_size, chunk_size = struct.unpack_from('<HHI', data, offset)

        if chunk_size <= 0:
            break

        if chunk_type == RES_STRING_POOL_TYPE:
            strings = parse_string_pool(data, offset)

        elif chunk_type == RES_XML_START_ELEMENT_TYPE:
            # The attributes come after the node header and the attribute extension
            attribute_start, attribute_size, attribute_count = struct.unpack_from('<HHH', data, offset + 24)

            attributes: dict[str, str | int | bool] = {}

            for index in range(attribute_count):
                attribute_offset = offset + 16 + attribute_start + index * attribute_size

                (
                    _namespace,
                    name,
                    _raw_value,
                    _value_size,
                    _reserved,
                    value_type,
                    value,
                ) = struct.unpack_from('<IIIHBBI', data, attribute_offset)

                if name == 0xFFFFFFFF or name >= len(strings):
                    continue

                attribute_name = strings[name]

                if value_type == TYPE_STRING:
                    if value < len(strings):
                        attributes[attribute_name] = strings[value]
                elif value_type in (TYPE_INT_DEC, TYPE_INT_HEX):
                    attributes[attribute_name] = value
                elif value_type == TYPE_INT_BOOLEAN:
                    attributes[attribute_name] = bool(value)
                elif value_type == TYPE_REFERENCE:
                    attributes[attribute_name] = value

            return attributes

        offset += chunk_size

    return {}


def read_apk_version(apk_path: Path | str) -> ApkVersion | None:
    """
    Reads the version of an apk from its manifest

    Returns None if the version could not be read
    """

    try:
        with zipfile.ZipFile(apk_path) as apk:
            with apk.open('AndroidManifest.xml') as manifest_file:
                manifest = manifest_file.read()
    except Exception:
        return None

    try:
        attributes = parse_manifest_attributes(manifest)
    except Exception:
        return None

    version_code = attributes.get('versionCode')
    version_name = attributes.get('versionName')

    if not isinstance(version_code, int):
        version_code = None
    if not isinstance(version_name, str):
        version_name = None

    if version_code is None and version_name is None:
        return None

    return ApkVersion(
        version_code = version_code,
        version_name = version_name,
    )

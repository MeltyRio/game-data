from typing import TypedDict, Literal, NotRequired, Any

type UpdateType = Literal['app', 'content']

class DiscordField(TypedDict):
    name: str
    value: str
    inline: bool

class DiscordEmbedFooter(TypedDict):
    text: str
    icon_url: NotRequired[str]
    proxy_icon_url: NotRequired[str]

class DiscordEmbedImage(TypedDict):
    url: str
    proxy_url: NotRequired[str]
    height: NotRequired[int]
    width: NotRequired[int]
    content_type: NotRequired[str]
    placeholder: NotRequired[str]
    placeholder_version: NotRequired[int]
    description: NotRequired[str]
    flags: NotRequired[int]

class DiscordEmbed(TypedDict):
    title: NotRequired[str]
    description: NotRequired[str]
    url: NotRequired[str]
    timestamp: NotRequired[str]
    color: NotRequired[int]
    image: NotRequired[DiscordEmbedImage]
    thumbnail: NotRequired[DiscordEmbedImage]
    fields: NotRequired[list[DiscordField]]

class DiscordComponent(TypedDict):
    type: int
    id: NotRequired[int]
    style: NotRequired[int]
    label: NotRequired[str]
    emoji: NotRequired[str]
    custom_id: NotRequired[str]
    sku_id: NotRequired[str]
    url: NotRequired[str]
    disabled: NotRequired[bool]

class DiscordMessage(TypedDict):
    content: str
    embeds: list[NotRequired[DiscordEmbed]]
    components: list[DiscordComponent]

class DiscordUser(TypedDict):
    username: str
    avatar: str

class DiscordConfig(TypedDict):
    name: str
    webhook: str
    user: DiscordUser
    message: dict[UpdateType, DiscordMessage]
    disabled: NotRequired[bool]

class NtfyConfig(TypedDict):
    name: str
    token: NotRequired[str]
    topic: str
    message: dict[UpdateType, dict[str, Any]]
    disabled: NotRequired[bool]


class NotificationConfig(TypedDict):
    discord: NotRequired[list[DiscordConfig]]
    ntfy: NotRequired[list[NtfyConfig]]


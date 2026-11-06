import json
import os
import random
from typing import Literal
import uuid

import requests

from .console import console
from .env import is_dev
from .notifyTypes import NotificationConfig, UpdateType


class Notifier:
    config: NotificationConfig | None = None
    
    def __init__(
        self,
        version: str = '',
        release_notes: str = '',
        app_icon: str | None = None,
    ) -> None:
        self.version: str = version
        self.release_notes: str = release_notes
        self.app_icon = app_icon

        self.get_notification_config()


    def get_notification_config(self):
        raw_config = os.environ.get('NOTIFICATION_CONFIG')
        if not raw_config:
            config_file = 'notifications.dev.json' if is_dev() else 'notifications.json'
            if not os.path.exists(config_file):
                return
            
            with open(config_file, 'r', encoding = 'utf-8') as file:
                raw_config = file.read()
        
        self.config = json.loads(raw_config)
    
    def format_string[T: str | dict | list](self, string: T) -> T:
        if isinstance(string, str):
            return string.format(
                version = self.version,
                release_notes = self.release_notes,
                app_icon = self.app_icon,
            ) # type: ignore
        elif isinstance(string, dict):
            new = {}
            for key, value in string.items():
                if isinstance(value, (str, dict, list)):
                    value = self.format_string(value)
                new[key] = value
            return new # type: ignore
        elif isinstance(string, list):
            new = []
            for value in string:
                if isinstance(value, (str, dict, list)):
                    value = self.format_string(value)
                new.append(value)
            return new # type: ignore
        
        return string
    
    def notify(self, type: UpdateType):
        if not self.config:
            return
        
        self.notify_ntfy(type)
        self.notify_discord(type)
        
    def notify_ntfy(self, type: UpdateType):
        if not self.config:
            return
        
        for ntfy_config in self.config.get('ntfy', []):
            message_config = ntfy_config['message'].get(type)
            if not message_config:
                continue
            if message_config.get('disabled'):
                console.print(f'Skipping {ntfy_config["name"]}')
                continue
            
            console.print(f'[yellow]ntfy {ntfy_config["name"]}[/]')
            
            body = {
                'topic': ntfy_config['topic'],
                **self.format_string(message_config),
            }


            headers = {
                'Content-Type': 'application/json'
            }
            if 'token' in ntfy_config:
                headers['Authorization'] = f'Bearer {ntfy_config["token"]}'

            failed = False
            try:
                response = requests.post('https://ntfy.sh/', data = json.dumps(body).encode(), headers = headers)
                failed = not response.ok
            except requests.HTTPError as e:
                failed = True
                console.print(e)
            
            if failed:
                console.print(body)
                console.print(f'[red]Failed to send to {ntfy_config['name']}[/]')

            
    
    def notify_discord(self, type: UpdateType):
        if not self.config:
            return
        
        for server in self.config.get('discord', []):
            message_config = server['message'].get(type)
            if not message_config or not server.get('webhook'):
                continue
            if message_config.get('disabled'):
                console.print(f'Skipping {ntfy_config["name"]}')
                continue
            
            console.print(f'Notifying [yellow]{server['name']}[/]')

            body = {
                "username": self.format_string(server['user']['username']),
                'content': self.format_string(message_config['content']),
                "avatar_url": self.format_string(server['user']['avatar']),
                "allowed_mentions": {
                    "parse": ["roles"],
                    "roles": []
                },
            }

            for embed in message_config.get('embeds', []):
                body.setdefault('embeds', []).append(self.format_string(dict(embed)))
            
            for component in message_config.get('components', []):
                body.setdefault('components', []).append(self.format_string(dict(component)))
            
            failed = False
            try:
                response = requests.post(f'{server["webhook"]}?with_components=true', json = body)
                failed = not response.ok
            except requests.HTTPError as e:
                failed = True
                console.print(e)
            
            if failed:
                console.print(f'[red]Failed to send to {server['name']}[/]')
            
            

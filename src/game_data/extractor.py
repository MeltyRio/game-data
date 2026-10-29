import os

from luna_kit.ark import ARK
from luna_kit.ark_filename import sort_ark_filenames
from luna_kit.texatlas import TexAtlas

from .console import console, track, COLUMNS
from rich.progress import Progress, TextColumn, BarColumn, MofNCompleteColumn, TimeRemainingColumn
from glob import glob
from pathlib import Path

def extract(arks_dir: Path, output: Path, aes_key: bytes | None = None):
    arks_dir = Path(arks_dir)
    output = Path(output)

    arks = os.listdir(arks_dir)
    try:
        arks = sort_ark_filenames(arks)
    except:
        console.print('[red]Could not sort ark files[/]')

    for filename in arks:
        with ARK(arks_dir/filename, aes_key = aes_key) as ark:
            console.print(f'Extracting [yellow]{filename}[/]')

            for filename in track(
                ark.namelist(),
                description = 'Extracting...',
                transient = True,
            ):
                try:
                    ark.extract(filename, output)
                except Exception as e:
                    e.add_note(f'filename: {filename}')
                    console.print(e)
    

    console.print('Splitting texatlas files')

    atlas_files = list(output.glob('*/**.texatlas'))

    with Progress(*COLUMNS, console = console, transient = True) as progress:
        atlas_progress = progress.add_task('Splitting...')
        files_progress = progress.add_task('Splitting...', total = len(atlas_files))

        for atlas_filename in atlas_files:
            timestamp = os.path.getmtime(atlas_filename)
            atlas = TexAtlas(atlas_filename)
            progress.update(
                atlas_progress,
                description = os.path.basename(atlas_filename),
                total = len(atlas.images),
                completed = 0,
            )
            for image in atlas.images:
                image_filename = output/image.filename
                if not image_filename.exists():
                    image_filename.parent.mkdir(parents = True, exist_ok = True)
                    image.image.save(image_filename)
                    os.utime(image_filename, (timestamp, timestamp))

                progress.update(atlas_progress, advance = 1)
            progress.update(files_progress, advance = 1)
            
    console.print('[green]Done![/]')


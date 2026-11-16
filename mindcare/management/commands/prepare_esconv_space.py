"""Prepare a reviewable Space upload, without publishing or touching secrets."""
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Bundle the ESConv Space app and shared contract for a separate deployment.'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True, type=Path)

    def handle(self, *args, **options):
        output = options['output'].resolve()
        if output.exists() and any(output.iterdir()):
            raise CommandError('Choose an empty output directory.')
        output.mkdir(parents=True, exist_ok=True)
        source = settings.BASE_DIR / 'deploy' / 'huggingface_space'
        for name in ('app.py', 'requirements.txt', 'README.md'):
            shutil.copyfile(source / name, output / name)
        shutil.copyfile(settings.BASE_DIR / 'mindcare' / 'chat_contract.py', output / 'chat_contract.py')
        self.stdout.write(self.style.SUCCESS(f'Prepared 4 files in {output}. No files were uploaded.'))

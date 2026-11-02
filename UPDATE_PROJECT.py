"""Grand Support Copilot safe in-place archive updater (Python 3.11+).

Double-click the separately provided .pyw copy to select a downloaded release
archive and the already installed Grand Support Copilot directory. No manual
unzipping required. Database, virtual environment and custom env stay intact.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile

ROOT_NAME = 'grand-support-copilot'
KEEP_DIRS = {'node_modules', '.venv', '__pycache__', '.git', 'dist', 'build', '.copilot-updates', '.pytest_cache'}
KEEP_FILES = {'copilot.sqlite3', '.env', '.env.local', '.env.production', '.env.development', '.DS_Store'}
ALLOW_FOLDERS = {'backend', 'frontend', 'scripts', 'tests', '.github'}
ALLOW_ROOT = {
    'UPDATE_PROJECT.py', 'UPDATE_PROJECT.bat', 'START_HERE_RU.md',
    'README.md', 'ANNOTATION_GUIDE_RU.md', 'RULES_FULL_RU.md', 'Makefile', '.gitignore',
    '.env.example', 'docker-compose.yml', 'setup_windows.bat',
    'start_backend.bat', 'start_frontend.bat', 'test_backend.bat',
    'START_DOCKER.bat', 'UPDATE_FROM_GIT.bat', 'RELEASE_NOTES_RU.md', 'CREATE_ADMIN_ACCOUNT.bat',
}


def is_source_file(parts: tuple[str, ...]) -> bool:
    if not parts or any(x in {'.', '..', ''} for x in parts):
        return False
    if any(part in KEEP_DIRS for part in parts):
        return False
    if parts[-1] in KEEP_FILES or parts[-1].endswith(('.pyc', '.sqlite3', '.db', '.log')):
        return False
    if len(parts) == 1:
        return parts[0] in ALLOW_ROOT
    return parts[0] in ALLOW_FOLDERS


def update(archive: Path, project: Path) -> tuple[int, Path]:
    archive = archive.expanduser().resolve()
    project = project.expanduser().resolve()
    if not (project / 'frontend' / 'package.json').is_file() or not (project / 'backend' / 'app' / 'main.py').is_file():
        raise ValueError('Выбери существующую папку grand-support-copilot, содержащую frontend и backend.')
    if not archive.is_file():
        raise FileNotFoundError(f'Архив не найден: {archive}')
    if not zipfile.is_zipfile(archive):
        raise ValueError('Выбранный файл не является ZIP-архивом.')
    items: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    with zipfile.ZipFile(archive) as z:
        invalid = z.testzip()
        if invalid:
            raise ValueError(f'Повреждён архив: {invalid}')
        for info in z.infolist():
            if info.is_dir():
                continue
            raw = PurePosixPath(info.filename.replace('\\', '/'))
            parts = raw.parts
            if not parts or parts[0] != ROOT_NAME:
                continue
            inner = parts[1:]
            if not is_source_file(inner):
                continue
            if info.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError('Архив с символическими ссылками не поддерживается.')
            items.append((info, inner))
        if len(items) < 12 or not any(parts[-2:] == ('app', 'main.py') for _, parts in items):
            raise ValueError('ZIP не похож на полный релиз Grand Support Copilot.')
        # Stage all contents BEFORE replacing any file. Thus a broken ZIP cannot
        # leave a partially installed release.
        with tempfile.TemporaryDirectory(prefix='copilot-update-') as staging:
            staged = Path(staging)
            for info, parts in items:
                target = staged.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            backup = project / '.copilot-updates' / ('backup-' + stamp)
            if backup.exists():
                raise FileExistsError(f'Резервная копия уже существует: {backup}')
            changed = 0
            for _, parts in items:
                source = staged.joinpath(*parts)
                dest = project.joinpath(*parts)
                if dest.exists() and dest.read_bytes() == source.read_bytes():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    back = backup.joinpath(*parts)
                    back.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, back)
                tmp = dest.with_name(dest.name + '.copilot-update-tmp')
                shutil.copy2(source, tmp)
                tmp.replace(dest)
                changed += 1
    return changed, backup


def locate_zip(script_dir: Path) -> Path | None:
    choices: list[Path] = []
    for folder in (script_dir, Path.home() / 'Downloads'):
        if folder.is_dir():
            choices += list(folder.glob('grand-support-copilot-*.zip'))
    return max(choices, key=lambda p: p.stat().st_mtime) if choices else None


def locate_project(script_dir: Path) -> Path | None:
    for folder in (script_dir, script_dir / ROOT_NAME, Path.cwd(), Path.cwd() / ROOT_NAME):
        if (folder / 'frontend' / 'package.json').is_file() and (folder / 'backend' / 'app' / 'main.py').is_file():
            return folder
    return None


def main():
    parser = argparse.ArgumentParser(description='Update an existing Grand Support Copilot installation in-place')
    parser.add_argument('--zip', dest='archive', type=Path)
    parser.add_argument('--project', type=Path)
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()
    script_dir = Path(__file__).resolve().parent
    archive = args.archive or locate_zip(script_dir)
    project = args.project or locate_project(script_dir)
    use_gui = not args.quiet and (not args.archive or not args.project)
    root = None
    try:
        if use_gui:
            import tkinter as tk
            from tkinter import filedialog, messagebox
            root = tk.Tk()
            root.withdraw()
            if not archive or not messagebox.askyesno('Grand Copilot · обновление', f'Использовать архив?\n{archive}'):
                name = filedialog.askopenfilename(title='Выбери скачанный архив с обновлением', filetypes=[('ZIP архив', '*.zip')])
                archive = Path(name) if name else None
            if not archive:
                return 2
            if not project or not messagebox.askyesno('Grand Copilot · папка проекта', f'Обновить эту папку?\n{project}'):
                folder = filedialog.askdirectory(title='Выбери уже установленную папку grand-support-copilot')
                project = Path(folder) if folder else None
            if not project:
                return 2
            if not messagebox.askyesno('Подтверждение', f'Обновить файлы здесь?\n{project}\n\nSQLite и зависимости сохранятся. Изменённые файлы будут скопированы в резервную папку.'):
                return 2
        if not archive or not project:
            parser.error('Необходимо указать --zip и --project (или запустить графическую версию).')
        changed, backup = update(archive, project)
        result = (f'Готово! Обновлено файлов: {changed}.\n'
                  f'Резервная копия прежних файлов: {backup}\n'
                  'Открой браузер и обнови страницу (Ctrl+F5).\n'
                  'При запущенных start_backend.bat и start_frontend.bat серверы автоматически подхватят изменения.')
        print(result)
        if root:
            messagebox.showinfo('Grand Copilot · успешно', result)
        return 0
    except Exception as exc:
        print(f'Ошибка обновления: {exc}', file=sys.stderr)
        if root:
            messagebox.showerror('Grand Copilot · ошибка', str(exc))
        return 1
    finally:
        if root:
            root.destroy()


if __name__ == '__main__':
    raise SystemExit(main())

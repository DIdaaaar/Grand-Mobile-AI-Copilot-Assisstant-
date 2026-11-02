import sys,tempfile,shutil,unittest,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from UPDATE_PROJECT import update

class UpdaterTests(unittest.TestCase):
    def test_updates_code_in_place_preserves_database_and_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            project=root/'grand-support-copilot'
            (project/'backend'/'app').mkdir(parents=True)
            (project/'frontend').mkdir()
            (project/'frontend'/'node_modules').mkdir()
            (project/'backend'/'app'/'main.py').write_text('old')
            (project/'frontend'/'package.json').write_text('old')
            (project/'backend'/'copilot.sqlite3').write_bytes(b'important-data')
            (project/'frontend'/'node_modules'/'keep.txt').write_text('installed')
            (project/'backend'/'.env').write_text('a secret')
            archive=root/'release.zip'
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('grand-support-copilot/backend/app/main.py','new')
                z.writestr('grand-support-copilot/frontend/package.json','new')
                for idx in range(12):z.writestr(f'grand-support-copilot/scripts/example_{idx}.py',str(idx))
                z.writestr('grand-support-copilot/backend/copilot.sqlite3','should-not-overwrite')
                z.writestr('grand-support-copilot/backend/.env','should-not-overwrite')
                z.writestr('grand-support-copilot/frontend/node_modules/keep.txt','should-not-overwrite')
            changed,backup=update(archive,project)
            self.assertEqual(changed,14)
            self.assertEqual((project/'backend'/'app'/'main.py').read_text(),'new')
            self.assertEqual((project/'backend'/'copilot.sqlite3').read_bytes(),b'important-data')
            self.assertEqual((project/'backend'/'.env').read_text(),'a secret')
            self.assertEqual((project/'frontend'/'node_modules'/'keep.txt').read_text(),'installed')
            self.assertEqual((backup/'backend'/'app'/'main.py').read_text(),'old')

if __name__=='__main__':unittest.main()

"""Build/run the read-only exact Alpha7 status bridge using installed local bytes."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

HERE = Path(__file__).resolve().parent
VERSION = '2027.0.0-alpha-7'
DEFAULT_INSTALL = Path(r'C:\Users\Public\wpilib\2027_alpha7')


def dependencies(install):
    jars, sources, natives = [], [], []
    for component in ('ntcore','wpiutil','datalog'):
        base = install / f'maven/org/wpilib/{component}/{component}-java/{VERSION}'
        jars.append(base / f'{component}-java-{VERSION}.jar')
        sources.append(base / f'{component}-java-{VERSION}-sources.jar')
        natives.append(install / f'maven/org/wpilib/{component}/{component}-cpp/{VERSION}/{component}-cpp-{VERSION}-windowsx86-64.zip')
    jars.append(install / 'maven/com/google/code/gson/gson/2.13.1/gson-2.13.1.jar')
    natives.append(install / f'maven/org/wpilib/wpinet/wpinet-cpp/{VERSION}/wpinet-cpp-{VERSION}-windowsx86-64.zip')
    return jars, sources, natives


def prepare(install=DEFAULT_INSTALL):
    if os.name != 'nt':
        raise RuntimeError('Only the locked Windows x86-64 Alpha7 native status profile is qualified')
    install = Path(install)
    jars, sources, natives = dependencies(install)
    lock = json.loads((HERE / 'dependencies.lock.json').read_text(encoding='utf-8'))
    files = jars + sources + natives
    if any(not p.is_file() for p in files):
        raise RuntimeError('Pinned installed status bridge dependencies unavailable; no downloads attempted')
    actual = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    if actual != lock['sha256']:
        raise RuntimeError('Status bridge dependency hash mismatch; explicit profile review required')
    java, javac = install / 'jdk/bin/java.exe', install / 'jdk/bin/javac.exe'
    version = subprocess.run([str(java),'-version'],capture_output=True,text=True,check=True,timeout=10)
    if 'version "25' not in version.stderr:
        raise RuntimeError('Status bridge requires the installed Java25 toolchain')
    classes, native = HERE / 'build/classes', HERE / 'build/native'
    classes.mkdir(parents=True,exist_ok=True)
    native.mkdir(parents=True,exist_ok=True)
    for archive in natives:
        with zipfile.ZipFile(archive) as package:
            for name in package.namelist():
                if name.endswith('.dll'):
                    target = native / Path(name).name
                    content = package.read(name)
                    # Windows locks loaded DLLs against writes. A second local
                    # reader can reuse the exact pinned bytes without rewriting
                    # the libraries already used by another NT process.
                    if not target.is_file() or target.read_bytes() != content:
                        target.write_bytes(content)
    classpath = os.pathsep.join(str(p) for p in jars)
    subprocess.run([str(javac),'-cp',classpath,'-d',str(classes),
                    *map(str,sorted((HERE / 'src').glob('*.java')))],check=True,timeout=30,
                   capture_output=True,text=True)
    env = os.environ.copy()
    env['PATH'] = str(native) + os.pathsep + env.get('PATH','')
    command = [str(java),'--enable-native-access=ALL-UNNAMED',f'-Djava.library.path={native}',
               '-cp',str(classes)+os.pathsep+classpath]
    return command, env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','bridge','synthetic-publisher'))
    parser.add_argument('--install',type=Path,default=DEFAULT_INSTALL)
    parser.add_argument('--host',help='Explicit configured NT host; never inferred from team number')
    parser.add_argument('--port',type=int,help='Explicit NT port')
    args = parser.parse_args()
    if args.action != 'build' and (args.port is None or not 1 <= args.port <= 65535):
        parser.error('Explicit port between 1 and 65535 required')
    if args.action == 'bridge' and not args.host:
        parser.error('Explicit configured host required')
    command, env = prepare(args.install)
    if args.action == 'build':
        print('Status bridge compiled: locked Alpha7/Java25 Windows x86-64; no connection opened')
    else:
        tail = ['StatusBridge',args.host,str(args.port)] if args.action == 'bridge' else ['SyntheticPublisher',str(args.port)]
        raise SystemExit(subprocess.call(command+tail,env=env))


if __name__ == '__main__':
    try:
        main()
    except (OSError,RuntimeError,subprocess.SubprocessError) as error:
        print('Status bridge tool failed: '+str(error),file=sys.stderr)
        raise SystemExit(1)

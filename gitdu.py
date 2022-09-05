# Originally inspired by https://gist.github.com/nk9/b150542ef72abc7974cb by Nick Kocharhook
from subprocess import check_output, CalledProcessError
import argparse
import glob
import signal
import sys
import re
import os


VERBOSE = False


def info(*args):
    if VERBOSE:
        print(*args, file=sys.stderr)


class Entry:
    def __init__(self, index_parts):
        self.sha1 = index_parts[0]
        self.type = index_parts[1]
        self.size = int(index_parts[2])
        self.pack_size = int(index_parts[3])
        self.offset = int(index_parts[4])
        self.depth = int(index_parts[5]) if index_parts[5] else 0
        self.base_sha1 = index_parts[6]
        self.path = None
        self.base_entry = None

    def __str__(self):
        return f"{self.sha1} {self.type} {self.size} {self.pack_size} {self.offset}"


def get_git_root_path():
    try:
        return check_output("git rev-parse --git-dir", text=True).strip()
    except CalledProcessError as e:
        if e.output:
            sys.stderr.write(e.output)
        return None


def get_git_verify_pack(git_root):
    idx_files = " ".join(glob.glob(f"{git_root}/objects/pack/pack-*.idx"))
    return check_output(f"git verify-pack -v {idx_files}", text=True)


def parse_git_verify_pack(verify_pack):
    rexp = re.compile(r"([0-9a-f]{40}) +(\w+) +(\d+) +(\d+) +(\d+)(?: +(\d+) +([0-9a-f]{40}))?")
    entries = {}
    for line in verify_pack.split("\n"):
        if m := rexp.match(line):
            entries[m.group(1)] = Entry(m.groups())
    for entry in entries.values():
        if entry.base_sha1:
            entry.base_entry = entries[entry.base_sha1]
    return entries


def get_git_rev_list():
    return check_output(f"git rev-list --all --objects", text=True)


def update_entries(entries, rev_list):
    for line in [s.strip() for s in rev_list.split("\n")]:
        if line:
            parts = line.split(maxsplit=1)
            if entry := entries.get(parts[0]):
                entry.path = parts[1] if len(parts) == 2 else ""
            else:
                info(f"Unpacked entry: {line}")


class DirEntry:
    def __init__(self, path, entry_type):
        self.path = path
        self.type = entry_type
        self.size = 0
        self.pack_size = 0
        self.updates = 0
        self.acc_size = 0
        self.acc_pack_size = 0
        self.acc_updates = 0

    def __str__(self):
        return "%11d %5d %-6s /%s" % (self.acc_pack_size, self.updates, self.type, self.path)

    def update_size(self, size, pack_size):
        self.size += size
        self.pack_size += pack_size
        self.updates += 1
        self.acc_size += size
        self.acc_pack_size += pack_size
        self.acc_updates += 1

    def update_acc_size(self, size, pack_size):
        self.acc_size += size
        self.acc_pack_size += pack_size
        self.acc_updates += 1


def make_dir_entries(entries, ignored_paths):
    dir_entries = {}
    for entry in entries.values():
        if entry.path is None:
            print(f"Not in rev-list: {entry}", file=sys.stderr)
            continue

        if entry.type in ("blob", "tree"):
            if entry.path in ignored_paths:
                continue
            dir_entry = dir_entries.get(entry.path)
            if not dir_entry:
                dir_entry = DirEntry(entry.path, entry.type)
                dir_entries[entry.path] = dir_entry
            dir_entry.update_size(entry.size, entry.pack_size)
            path = entry.path
            while path:
                path = os.path.dirname(path)
                parent_dir_entry = dir_entries.get(path)
                if not parent_dir_entry:
                    parent_dir_entry = DirEntry(path, "tree")
                    dir_entries[path] = parent_dir_entry
                parent_dir_entry.update_acc_size(entry.size, entry.pack_size)
    return sorted(dir_entries.values(), key=lambda e: e.path)


class ExtensionEntry:
    def __init__(self, extension):
        self.extension = extension
        self.files = set()
        self.acc_size = 0
        self.acc_pack_size = 0
        self.acc_updates = 0

    def __str__(self):
        return "%11d %5d %8d %s" % (self.acc_pack_size, len(self.files), self.acc_updates, self.extension)

    def update_size(self, path, size, pack_size):
        if path not in self.files:
            self.files.add(path)
        self.acc_size += size
        self.acc_pack_size += pack_size
        self.acc_updates += 1


def make_file_extension_entries(entries, path, ignored_paths):
    ext_entries = {}
    for entry in entries.values():
        if entry.path is None:
            print(f"Not in rev-list: {entry}", file=sys.stderr)
            continue

        if entry.type != "blob":
            continue

        if path:
            if not entry.path.startswith(path):
                continue
            if entry.path != path and entry.path[len(path)] != "/":
                continue

        if entry.path in ignored_paths:
            continue

        ext = os.path.splitext(entry.path)[1]
        ext_entry = ext_entries.get(ext)
        if not ext_entry:
            ext_entry = ExtensionEntry(ext)
            ext_entries[ext] = ext_entry
        ext_entry.update_size(entry.path, entry.size, entry.pack_size)
    return sorted(ext_entries.values(), key=lambda e: e.acc_pack_size)


def parse_arguments():
    parser = argparse.ArgumentParser(description='List the size of files and folders in a git repository')
    parser.add_argument("PATH", nargs="?", help="Something")
    parser.add_argument("-a", "--all", action="store_true", default=False, help="List files as well as directories")
    parser.add_argument("-d", "--max-depth", type=int, default=sys.maxsize,
                        help="Print the total for a directory or file only if it is N or fewer levels below the command"
                             " line argument.")
    parser.add_argument("-t", "--threshold", type=int,
                        help="Exclude entries smaller than SIZE if positive, or entries greater than SIZE if negative")
    parser.add_argument("-e", "--extensions", action="store_true",
                        help="Report the accumulated size of file extensions rather than folders and files"
                             " (PATH, -a and -d options are ignored).")
    parser.add_argument("--vp", "--verify-pack", dest="verify_pack",
                        help="Set the name of the file containing output from verify-pack command."
                             " If the file exists, its contents will be used and the command will not be executed."
                             " If it doesn't exist, the command will be executed and its output is"
                             " written to the file.")
    parser.add_argument("--rl", "--rev-list", dest="rev_list",
                        help="Set the name of the file containing output from rev-list command."
                             " If the file exists, its contents will be used and the command will not be executed."
                             " If it doesn't exist, the command will be executed and its output is"
                             " written to the file.")
    parser.add_argument("--filter", metavar="FILE", help="FILE is a file containing  paths that will be removed from the report.")
    parser.add_argument("-q", "--quiet", action="store_false", default="true", dest="verbose")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def signal_handler(sig, frame):
    print('Caught Ctrl-C. Exiting.')
    sys.exit(0)


def get_relative_path():
    top_level = check_output("git rev-parse --show-toplevel", text=True).strip()
    cur_dir = os.getcwd()
    return os.path.relpath(cur_dir, top_level).replace("\\", "/")


def main():
    args = parse_arguments()

    signal.signal(signal.SIGINT, signal_handler)

    global VERBOSE
    VERBOSE = args.verbose

    git_root = get_git_root_path()
    if not git_root:
        return 1

    if args.verify_pack and os.path.exists(args.verify_pack):
        info("Reading cached verify-pack output.")
        verify_pack = open(args.verify_pack).read()
    else:
        info("Running verify-pack.")
        verify_pack = get_git_verify_pack(git_root)
        if args.verify_pack:
            open(args.verify_pack, "w").write(verify_pack)

    info("Parsing verify-pack output.")
    entries = parse_git_verify_pack(verify_pack)

    if args.rev_list and os.path.exists(args.rev_list):
        info("Reading cached rev-list output.")
        rev_list = open(args.rev_list).read()
    else:
        info("Running rev-list.")
        rev_list = get_git_rev_list()
        if args.rev_list:
            open(args.rev_list, "w").write(rev_list)

    info("Parsing rev-list output.")
    update_entries(entries, rev_list)

    ignored_paths = set()
    if args.filter:
        info("Reading filter file.")
        for line in (l.strip() for l in open(args.filter)):
            if not line:
                continue
            if line.startswith("./"):
                line = line[2:]
            ignored_paths.add(line)

    max_depth = args.max_depth
    list_files = args.all
    threshold = args.threshold
    path = get_relative_path()
    if args.PATH:
        path = os.path.join(path, args.PATH).replace("\\", "/")
    if path.startswith("."):
        path = path[1:]
    if path.startswith("/"):
        path = path[1:]
    if not args.extensions:
        for entry in make_dir_entries(entries, ignored_paths):
            if not list_files and entry.type != "tree":
                continue
            if entry.path.count("/") >= max_depth:
                continue
            if threshold:
                if threshold < 0 and entry.acc_pack_size > -threshold:
                    continue
                if threshold > 0 and entry.acc_pack_size < threshold:
                    continue
            if path:
                if not entry.path.startswith(path):
                    continue
                if entry.path != path and entry.path[len(path)] != "/":
                    continue
            print(entry)
    else:
        for entry in make_file_extension_entries(entries, path, ignored_paths):
            if threshold:
                if threshold < 0 and entry.acc_pack_size > -threshold:
                    continue
                if threshold > 0 and entry.acc_pack_size < threshold:
                    continue
            print(entry)

    return 0


# Default function is main()
if __name__ == '__main__':
    sys.exit(main())

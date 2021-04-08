# Originally inspired by https://gist.github.com/nk9/b150542ef72abc7974cb by Nick Kocharhook
from subprocess import check_output, CalledProcessError
import argparse
import glob
import signal
import sys
import re
import os


VERBOSE = False


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
            elif VERBOSE:
                print(f"Unpacked entry: {line}", file=sys.stderr)


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


def make_dir_entries(entries):
    dir_entries = {}
    for entry in entries.values():
        if entry.path is None:
            print(f"Not in rev-list: {entry}", file=sys.stderr)
            continue

        if entry.type in ("blob", "tree"):
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


def parse_arguments():
    parser = argparse.ArgumentParser(description='List the size of files and folders in a git repository')
    parser.add_argument("PATH", nargs="?", help="Something")
    parser.add_argument("-a", "--all", action="store_true", default=False, help="List files as well as directories")
    parser.add_argument("-d", "--max-depth", type=int, default=sys.maxsize,
                        help="Print the total for a directory or file only if it is N or fewer levels below the command"
                             " line argument.")
    parser.add_argument("-t", "--threshold", type=int,
                        help="Exclude entries smaller than SIZE if positive, or entries greater than SIZE if negative")
    parser.add_argument('--verify-pack',
                        help="Set the name of the file containing output from verify-pack command."
                             " If the file exists, its contents will be used and the command will not be executed."
                             " If it doesn't exist, the command will be executed and its output is"
                             " written to the file.")
    parser.add_argument('--rev-list',
                        help="Set the name of the file containing output from rev-list command."
                             " If the file exists, its contents will be used and the command will not be executed."
                             " If it doesn't exist, the command will be executed and its output is"
                             " written to the file.")
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
        if VERBOSE:
            print("Reading cached verify-pack output.", file=sys.stderr)
        verify_pack = open(args.verify_pack).read()
    else:
        if VERBOSE:
            print("Running verify-pack.", file=sys.stderr)
        verify_pack = get_git_verify_pack(git_root)
        if args.verify_pack:
            open(args.verify_pack, "w").write(verify_pack)

    if VERBOSE:
        print("Parsing verify-pack output.", file=sys.stderr)
    entries = parse_git_verify_pack(verify_pack)

    if args.rev_list and os.path.exists(args.rev_list):
        if VERBOSE:
            print("Reading cached rev-list output.", file=sys.stderr)
        rev_list = open(args.rev_list).read()
    else:
        if VERBOSE:
            print("Running rev-list.", file=sys.stderr)
        rev_list = get_git_rev_list()
        if args.rev_list:
            open(args.rev_list, "w").write(rev_list)

    if VERBOSE:
        print("Parsing rev-list output.", file=sys.stderr)
    update_entries(entries, rev_list)

    max_depth = args.max_depth
    list_files = args.all
    threshold = args.threshold
    path = get_relative_path()
    if args.PATH:
        path = os.path.join(path, args.PATH).replace("\\", "/")
    if path == ".":
        path = ""
    elif path[0] == "/":
        path = path[1:]
    for entry in make_dir_entries(entries):
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
    return 0


# Default function is main()
if __name__ == '__main__':
    sys.exit(main())

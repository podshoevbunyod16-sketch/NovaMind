"""
sandbox_shell.py — маленький безопасный «shell» для песочницы.

Команды по-прежнему запускаются БЕЗ /bin/sh: строку разбираем сами и
запускаем программы напрямую через subprocess. Зато теперь работает то,
без чего терминал был полуживым:

    curl -s "https://api.github.com/repos/x/y?per_page=5&page=2" | jq .name
    git add . && git commit -m "first"
    python3 make.py > out.txt 2>&1 ; cat out.txt
    cd src && ls *.py
    pip install requests || echo "нет сети"

Что поддерживается
  * конвейеры `|`, цепочки `&&`, `||`, `;` и перевод строки;
  * перенаправления `>`, `>>`, `<`, `2>`, `2>>`, `2>&1`, `&>`, `>&2`;
  * кавычки '…' и "…", экранирование `\\`, комментарии `#`;
  * переменные `$HOME`, `${PWD}`, `NAME=значение команда`, `export NAME=…`;
  * маски `*.py`, `data/??.csv`, `~` (домашняя папка = workspace);
  * встроенные `cd`, `export`, `nova …` (nova можно направить в конвейер).

Чего нет (и не будет)
  * подстановки `$(…)` и `` `…` ``, фоновый запуск `&`, here-doc `<<`;
  * программ вне белого списка — каждая программа конвейера проверяется,
    в том числе спрятанная за env / timeout / xargs / find -exec;
  * записи за пределы рабочей папки: цели `>`, `cd`, аргументы rm / mv / cp /
    mkdir / touch / chmod / tee / sed -i, `curl -o`, `wget -O`, `git -C`,
    `tar -C`, `unzip -d` должны оставаться внутри workspace.

Это защита от ошибок, а не песочница ОС: python3/node в списке разрешённых
могут всё, что может процесс сервера. Поэтому терминал выключен по умолчанию
(TERMINAL_ENABLED=1) и доступен только вошедшим.
"""
import glob
import os
import re
import signal
import subprocess
import tempfile
import time

OPERATORS = ("2>&1", ">&2", "1>&2", "2>>", "&&", "||", ">>", "&>", "2>", "|", ";", ">", "<")
REDIRECTS = {">", ">>", "<", "2>", "2>>", "&>", "2>&1", ">&2", "1>&2"}
TAKES_TARGET = {">", ">>", "<", "2>", "2>>", "&>"}
SEPARATORS = {"&&", "||", ";"}
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
GLOB_CHARS = set("*?[")

# Обёртки, которые запускают другую программу — её тоже проверяем
WRAPPERS = {"env", "timeout", "nice", "nohup", "time", "xargs"}
# Команды, которые меняют файлы: их пути не должны выходить из workspace
WRITE_ALL_ARGS = {"rm", "rmdir", "mv", "touch", "mkdir", "chmod", "tee", "truncate", "ln", "unlink"}
# Сколько вывода держим на диске, пока команда работает (потом обрезаем до max_output)
SPILL_LIMIT = 8 * 1024 * 1024


class ShellError(ValueError):
    """Команду нельзя выполнить: синтаксис или запрещённая конструкция."""


class Word(str):
    """Слово команды. glob=True — в нём есть маска без кавычек."""
    glob = False

    @classmethod
    def make(cls, text, globbable=False):
        word = cls(text)
        word.glob = globbable
        return word


class Op(str):
    """Оператор: |, &&, >, 2>&1 …"""


# ───────────────────────── разбор строки ─────────────────────────

def tokenize(line, env):
    """Строка → список Word/Op. Переменные раскрываются сразу (из env)."""
    tokens = []
    i, n = 0, len(line)
    buf, has_word, globbable = [], False, False

    def flush():
        nonlocal buf, has_word, globbable
        if has_word:
            tokens.append(Word.make("".join(buf), globbable))
        buf, has_word, globbable = [], False, False

    def expand(pos):
        """$NAME / ${NAME} начиная с line[pos] == '$'. → (текст, новая позиция)."""
        if pos + 1 < n and line[pos + 1] == "(":
            raise ShellError("Подстановка $(…) не поддерживается — выполните команду отдельно")
        if pos + 1 < n and line[pos + 1] == "{":
            end = line.find("}", pos + 2)
            if end < 0:
                raise ShellError("Не закрыта скобка ${…}")
            name = line[pos + 2:end]
            if not NAME_RE.fullmatch(name):
                raise ShellError(f"Непонятная переменная ${{{name}}}")
            return env.get(name, ""), end + 1
        match = NAME_RE.match(line, pos + 1)
        if not match:
            return "$", pos + 1
        return env.get(match.group(0), ""), match.end()

    while i < n:
        ch = line[i]
        if ch in " \t\r":
            flush()
            i += 1
            continue
        if ch == "\n":
            flush()
            if tokens and tokens[-1] != ";":
                tokens.append(Op(";"))
            i += 1
            continue
        if ch == "#" and not has_word:
            while i < n and line[i] != "\n":
                i += 1
            continue
        if ch == "\\":
            if i + 1 < n and line[i + 1] == "\n":       # перенос строки внутри команды
                i += 2
                continue
            if i + 1 < n:
                buf.append(line[i + 1])
                has_word = True
            i += 2
            continue
        if ch == "'":
            end = line.find("'", i + 1)
            if end < 0:
                raise ShellError("Не закрыта одинарная кавычка")
            buf.append(line[i + 1:end])
            has_word = True
            i = end + 1
            continue
        if ch == '"':
            i += 1
            has_word = True
            while True:
                if i >= n:
                    raise ShellError("Не закрыта двойная кавычка")
                c = line[i]
                if c == '"':
                    i += 1
                    break
                if c == "\\" and i + 1 < n and line[i + 1] in '"\\$`\n':
                    if line[i + 1] != "\n":
                        buf.append(line[i + 1])
                    i += 2
                    continue
                if c == "`":
                    raise ShellError("Подстановка `…` не поддерживается — выполните команду отдельно")
                if c == "$":
                    text, i = expand(i)
                    buf.append(text)
                    continue
                buf.append(c)
                i += 1
            continue
        if ch == "`":
            raise ShellError("Подстановка `…` не поддерживается — выполните команду отдельно")
        if ch == "$":
            text, i = expand(i)
            buf.append(text)
            has_word = True
            continue
        if ch in "<>" and i + 1 < n and line[i + 1] == "(":
            raise ShellError("Подстановка процессов <(…) не поддерживается")
        if line.startswith("<<", i):
            raise ShellError("Here-doc (<<) не поддерживается — запишите текст в файл через write")
        # «2>» и «2>&1» — только если 2 стоит отдельным словом
        op = next((o for o in OPERATORS if line.startswith(o, i)
                   and (not o[0].isdigit() or not has_word)), None)
        if op:
            flush()
            tokens.append(Op(op))
            i += len(op)
            continue
        if ch == "&":
            raise ShellError("Фоновый запуск «&» не поддерживается — команды выполняются по очереди")
        if ch in GLOB_CHARS:
            globbable = True
        if ch == "~" and not has_word and (i + 1 >= n or line[i + 1] in "/ \t\n;|&<>"):
            buf.append(env.get("HOME", "~"))
            has_word = True
            i += 1
            continue
        buf.append(ch)
        has_word = True
        i += 1
    flush()
    return tokens


def parse(line, env):
    """Строка → список (разделитель, конвейер). Конвейер — список простых команд.

    Простая команда: {"assign": [...], "argv": [...], "redirects": [(op, цель)]}.
    Разделитель первой цепочки — None, дальше «;», «&&» или «||».
    """
    tokens = tokenize(line, env)
    chains, pipeline, current = [], [], None
    joiner = None

    def new_command():
        return {"assign": [], "argv": [], "redirects": []}

    def close_command():
        nonlocal current
        if current is None or not (current["argv"] or current["assign"]):
            raise ShellError("Пустая команда рядом с оператором")
        pipeline.append(current)
        current = None

    def close_pipeline(next_joiner):
        nonlocal pipeline, joiner
        close_command()
        chains.append((joiner, pipeline))
        pipeline, joiner = [], next_joiner

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, Op):
            if token in REDIRECTS:
                if current is None:
                    current = new_command()
                target = None
                if token in TAKES_TARGET:
                    index += 1
                    if index >= len(tokens) or isinstance(tokens[index], Op):
                        raise ShellError(f"После «{token}» нужен файл")
                    target = tokens[index]
                current["redirects"].append((str(token), target))
            elif token == "|":
                close_command()
            elif token in SEPARATORS:
                if token == ";" and current is None and not pipeline:
                    index += 1                       # «;;» и «; » в конце — просто пропускаем
                    continue
                close_pipeline(str(token))
            index += 1
            continue
        if current is None:
            current = new_command()
        if not current["argv"] and ASSIGN_RE.match(token):
            current["assign"].append(str(token))
        else:
            current["argv"].append(token)
        index += 1

    if current is not None:
        close_pipeline(None)
    elif pipeline:
        raise ShellError("Команда не может заканчиваться на «|»")
    elif joiner in ("&&", "||"):
        raise ShellError(f"Команда не может заканчиваться на «{joiner}»")
    if not chains:
        raise ShellError("Пустая команда")
    return chains


# ───────────────────────── проверки ─────────────────────────

def _skip_options(argv, start, with_value=()):
    """Пропускает опции (-x, --long, и опции со значением). → индекс первого аргумента."""
    i = start
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            return i + 1
        if not arg.startswith("-") or arg == "-":
            return i
        if arg in with_value:
            i += 2
            continue
        i += 1
    return i


def inner_programs(argv):
    """Программы, которые запустит команда (сама и через обёртки)."""
    found = []
    while argv:
        program = os.path.basename(argv[0])
        found.append(program)
        rest = list(argv[1:])
        if program == "find":
            for flag in ("-exec", "-execdir", "-ok", "-okdir"):
                for pos, arg in enumerate(rest):
                    if arg == flag and pos + 1 < len(rest):
                        found.extend(inner_programs(rest[pos + 1:pos + 2]))
            return found
        if program not in WRAPPERS:
            return found
        if program == "env":
            i = _skip_options(rest, 0, with_value=("-u", "--unset", "-C", "--chdir", "-S"))
            while i < len(rest) and ASSIGN_RE.match(rest[i]):
                i += 1
        elif program == "timeout":
            i = _skip_options(rest, 0, with_value=("-s", "--signal", "-k", "--kill-after"))
            i += 1                                   # длительность
        elif program == "nice":
            i = _skip_options(rest, 0, with_value=("-n", "--adjustment"))
        elif program == "xargs":
            i = _skip_options(rest, 0, with_value=("-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a"))
            if i >= len(rest):
                return found + ["echo"]
        else:                                        # nohup, time
            i = _skip_options(rest, 0)
        argv = rest[i:]
    return found


def check_programs(chains, allowed, builtins):
    """Все программы всех конвейеров — из белого списка. Иначе ShellError."""
    for _joiner, pipeline in chains:
        for position, command in enumerate(pipeline):
            argv = command["argv"]
            if not argv:
                continue
            program = os.path.basename(argv[0])
            if program in builtins:
                if program in ("cd", "export") and len(pipeline) > 1:
                    raise ShellError(f"«{program}» нельзя ставить в конвейер — используйте «&&»")
                if program == "nova" and position > 0:
                    raise ShellError("nova в конвейере может стоять только первой: nova search … | grep …")
                continue
            names = inner_programs([str(a) for a in argv])
            for name in names:
                if name not in allowed:
                    raise ShellError(f"Команда «{name}» не в списке разрешённых")
            if "xargs" in names and set(names[names.index("xargs") + 1:]) & (WRITE_ALL_ARGS | {"cp", "sed"}):
                # пути придут из stdin — проверить их заранее нельзя
                raise ShellError("xargs с rm/mv/cp/… отключён: перечислите файлы явно или используйте маску")


def write_targets(argv):
    """Пути, куда команда собирается писать. Проверяются на выход из workspace."""
    program = os.path.basename(argv[0])
    args = [str(a) for a in argv[1:]]
    plain = [a for a in args if not a.startswith("-")]
    targets = []
    if program in WRITE_ALL_ARGS:
        if program == "chmod" and plain:
            plain = plain[1:]                        # первый аргумент — режим
        targets += plain
    elif program == "cp":
        if "-t" in args and args.index("-t") + 1 < len(args):
            targets.append(args[args.index("-t") + 1])
        elif plain:
            targets.append(plain[-1])
    elif program == "sed" and any(a == "-i" or a.startswith("-i") or a == "--in-place" for a in args):
        targets += plain[1:]
    if program == "find" and {"-delete", "-exec", "-execdir", "-ok", "-okdir"} & set(args):
        for arg in args:                             # стартовые папки: до первого выражения
            if arg.startswith(("-", "(", "!")):
                break
            targets.append(arg)
        if not targets:
            targets.append(".")
    for flags, program_names in (
        (("-o", "--output"), ("curl",)),
        (("-O", "-P", "--output-document", "--directory-prefix"), ("wget",)),
        (("-C", "--directory"), ("tar",)),
        (("-d",), ("unzip",)),
        (("-C", "--git-dir", "--work-tree"), ("git",)),
    ):
        if program not in program_names:
            continue
        for pos, arg in enumerate(args):
            if arg in flags and pos + 1 < len(args):
                targets.append(args[pos + 1])
            for flag in flags:
                if flag.startswith("--") and arg.startswith(flag + "="):
                    targets.append(arg.split("=", 1)[1])
    return targets


# ───────────────────────── выполнение ─────────────────────────

class Shell:
    """Выполняет одну строку команд в рабочей папке.

    allowed  — белый список программ;
    builtins — {"имя": функция(argv) → (код, stdout, stderr)} (например, nova);
    """

    def __init__(self, workspace, allowed, env, builtins=None, max_output=12000):
        self.workspace = os.path.realpath(workspace)
        self.allowed = set(allowed)
        self.base_env = dict(env)
        self.builtins = dict(builtins or {})
        self.max_output = max_output

    # пути
    def inside(self, path, cwd):
        """Абсолютный путь внутри workspace или ShellError."""
        if path == "/dev/null":
            return path
        target = os.path.realpath(os.path.join(cwd, path))
        if target != self.workspace and not target.startswith(self.workspace + os.sep):
            raise ShellError(f"Путь «{path}» выходит за пределы рабочей папки")
        return target

    def expand_globs(self, argv, cwd):
        out = []
        for word in argv:
            if getattr(word, "glob", False):
                matches = sorted(glob.glob(os.path.join(cwd, word)))
                if matches:
                    out.extend(os.path.relpath(m, cwd) if not os.path.isabs(word) else m for m in matches)
                    continue
            out.append(str(word))
        return out

    def validate(self, line):
        """Синтаксис и белый список — до запуска чего-либо. → разобранные цепочки."""
        names = set(self.builtins) | {"cd", "export"}
        chains = parse(line, self.base_env)
        check_programs(chains, self.allowed, names)
        # Абсолютные пути не зависят от cd — их проверяем до запуска
        for _joiner, pipeline in chains:
            for command in pipeline:
                paths = [str(t) for _op, t in command["redirects"] if t is not None]
                if command["argv"]:
                    paths += write_targets(command["argv"])
                    if os.path.basename(str(command["argv"][0])) == "cd":
                        paths += [str(a) for a in command["argv"][1:2]]
                for path in paths:
                    if os.path.isabs(path) and not getattr(path, "glob", False):
                        self.inside(path, self.workspace)
        return chains

    def run(self, line, timeout=20):
        chains = self.validate(line)
        env = dict(self.base_env)
        cwd = self.workspace
        started = time.time()
        deadline = started + float(timeout)
        state = {"timed_out": False, "too_big": False}
        out = tempfile.TemporaryFile(buffering=0)     # без буфера: пишут и мы, и дочерние процессы
        err = tempfile.TemporaryFile(buffering=0)
        code = 0
        try:
            for joiner, pipeline in chains:
                if joiner == "&&" and code != 0:
                    continue
                if joiner == "||" and code == 0:
                    continue
                if time.time() >= deadline:
                    state["timed_out"] = True
                    break
                try:
                    code, cwd = self._run_pipeline(pipeline, env, cwd, out, err, deadline, state)
                except ShellError as exc:
                    err.write(f"{exc}\n".encode("utf-8"))
                    code = 1
                if state["timed_out"] or state["too_big"]:
                    break
            if state["too_big"]:
                err.write("\n… вывод слишком большой — команда остановлена".encode("utf-8"))
            return {
                "code": 124 if state["timed_out"] else code,
                "stdout": self._read(out),
                "stderr": self._read(err),
                "duration_ms": int((time.time() - started) * 1000),
                "timed_out": state["timed_out"],
            }
        finally:
            out.close()
            err.close()

    def _read(self, handle):
        size = os.fstat(handle.fileno()).st_size
        handle.seek(0)
        blob = handle.read(min(size, self.max_output * 4 + 4)) or b""
        text = blob.decode("utf-8", "replace")
        if len(text) > self.max_output or size > len(blob):
            return text[:self.max_output] + "\n… вывод обрезан"
        return text

    def _open_target(self, op, target, cwd):
        path = self.inside(str(target), cwd)
        if op == "<":
            if path != "/dev/null" and not os.path.isfile(path):
                raise ShellError(f"Нет файла «{target}»")
            return open(path, "rb", buffering=0)
        parent = os.path.dirname(path)
        if path != "/dev/null" and not os.path.isdir(parent):
            raise ShellError(f"Нет папки для «{target}»")
        if os.path.isdir(path):
            raise ShellError(f"«{target}» — это папка")
        return open(path, "ab" if op in (">>", "2>>") else "wb", buffering=0)

    def _run_pipeline(self, pipeline, env, cwd, out, err, deadline, state):
        # Одиночные встроенные: присваивания, cd, export
        if len(pipeline) == 1:
            command = pipeline[0]
            if not command["argv"]:
                for item in command["assign"]:
                    name, value = item.split("=", 1)
                    env[name] = value
                return 0, cwd
            program = os.path.basename(str(command["argv"][0]))
            if program == "cd":
                args = self.expand_globs(command["argv"][1:], cwd)
                target = self.inside(args[0] if args else self.workspace, cwd)
                if not os.path.isdir(target):
                    raise ShellError(f"cd: нет папки «{args[0] if args else target}»")
                env["PWD"] = target
                return 0, target
            if program == "export":
                for item in command["argv"][1:]:
                    if "=" in item:
                        name, value = str(item).split("=", 1)
                        if NAME_RE.fullmatch(name):
                            env[name] = value
                return 0, cwd

        processes, opened = [], []
        previous = None                   # stdout предыдущей программы
        status = 0                        # код последней программы конвейера
        final = None                      # процесс, чей код станет кодом конвейера
        try:
            for position, command in enumerate(pipeline):
                last = position == len(pipeline) - 1
                argv = self.expand_globs(command["argv"], cwd)
                local_env = dict(env)
                local_env["PWD"] = cwd
                for item in command["assign"]:
                    name, value = item.split("=", 1)
                    local_env[name] = value
                for path in write_targets(argv):
                    self.inside(path, cwd)

                stdin = previous if previous is not None else subprocess.DEVNULL
                stdout = out if last else subprocess.PIPE
                stderr = err
                for op, target in command["redirects"]:
                    if op == "<":
                        stdin = self._open_target(op, target, cwd)
                        opened.append(stdin)
                    elif op in (">", ">>"):
                        stdout = self._open_target(op, target, cwd)
                        opened.append(stdout)
                    elif op in ("2>", "2>>"):
                        stderr = self._open_target(op, target, cwd)
                        opened.append(stderr)
                    elif op == "&>":
                        stdout = stderr = self._open_target(">", target, cwd)
                        opened.append(stdout)
                    elif op == "2>&1":
                        stderr = subprocess.STDOUT
                    elif op in (">&2", "1>&2"):
                        stdout = err

                program = os.path.basename(argv[0])
                if program in self.builtins:          # nova: работает в процессе сервера
                    code, text, problem = self.builtins[program](argv)
                    blob = (text or "").encode("utf-8")
                    if problem:
                        target = err if stderr in (err, subprocess.STDOUT) else stderr
                        target.write((problem + "\n").encode("utf-8"))
                    if last:
                        if stdout is subprocess.PIPE:
                            stdout = out
                        stdout.write(blob)
                        status = code
                    else:
                        feed = tempfile.TemporaryFile(buffering=0)
                        feed.write(blob)
                        feed.seek(0)
                        opened.append(feed)
                        previous = feed
                    continue

                try:
                    process = subprocess.Popen(
                        argv, cwd=cwd, env=local_env, stdin=stdin, stdout=stdout, stderr=stderr,
                        start_new_session=True,
                    )
                except FileNotFoundError:
                    err.write(f"{program}: команда не установлена\n".encode("utf-8"))
                    status, previous = 127, None
                    continue
                except OSError as exc:
                    err.write(f"{program}: {exc}\n".encode("utf-8"))
                    status, previous = 126, None
                    continue
                if previous is not None and hasattr(previous, "close") and previous not in opened:
                    previous.close()          # родителю конец трубы больше не нужен
                previous = process.stdout if not last else None
                processes.append(process)
                if last:
                    final = process

            # Ждём все программы конвейера, следим за временем и размером вывода
            while any(p.poll() is None for p in processes):
                if time.time() >= deadline:
                    state["timed_out"] = True
                    break
                if (os.fstat(out.fileno()).st_size > SPILL_LIMIT
                        or os.fstat(err.fileno()).st_size > SPILL_LIMIT):
                    state["too_big"] = True
                    break
                time.sleep(0.02)
            if state["timed_out"] or state["too_big"]:
                for process in processes:
                    _kill(process)
            for process in processes:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    _kill(process)
                    process.wait()
            if final is not None:
                status = final.returncode if final.returncode is not None else -1
            return status, cwd
        finally:
            for process in processes:
                if process.stdout:
                    process.stdout.close()
            for handle in opened:
                try:
                    handle.close()
                except OSError:
                    pass


def _kill(process):
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass

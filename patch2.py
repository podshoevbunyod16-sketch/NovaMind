import os, re, shutil, py_compile

def patch(path, marker, pairs):
    if not os.path.exists(path):
        print("❌", path, "не найден"); return
    t = open(path, encoding="utf-8").read()
    if marker in t:
        print("⏭ ", path, "уже применено"); return
    new = t
    for old, rep in pairs:
        if old not in new:
            print("❌", path, "нужное место не найдено, файл не тронут"); return
        new = new.replace(old, rep, 1)
    shutil.copy(path, path + ".bak")
    open(path, "w", encoding="utf-8").write(new)
    try:
        py_compile.compile(path, doraise=True)
        print("✅", path)
    except py_compile.PyCompileError as e:
        shutil.copy(path + ".bak", path)
        print("❌ синтаксис, откатил:", e)

# 1) agent.py: когда шаги кончились, просим итоговый ответ без инструментов
patch("agenticseek/core/agent.py", "Лимит шагов исчерпан", [(
"        return self.limit_answer(), self.last_feedback\n",
'''        # шаги кончились: просим итог по уже собранному, без инструментов
        try:
            self.memory.push("user",
                "Лимит шагов исчерпан. НЕ вызывай инструменты. Дай итоговый ответ "
                "пользователю по тому, что уже найдено; если данных мало, скажи честно.")
            final = strip_unknown_blocks(self.llm_request(), set(self.tools))
            if str(final).strip():
                return final, self.last_feedback
        except AgentStopped:
            raise
        except Exception:  # noqa: BLE001
            pass
        return self.limit_answer(), self.last_feedback
''')])

# 2) nova_tools.py: поисковые инструменты первыми + подсказка модели
patch("agenticseek/tools/nova_tools.py", "sorted(tools, key", [
('''        listing = "; ".join(f"{t['server_id']}:{t['name']}" for t in tools[:40])''',
 '''        tools = sorted(tools, key=lambda t: 0 if "search" in t["name"] else 1)
        listing = "; ".join(f"{t['server_id']}:{t['name']}" for t in tools[:60])'''),
('''". Выбери подходящий сам, у пользователя server_id не спрашивай."''',
 '''". Выбери подходящий сам, у пользователя server_id не спрашивай. "
            "Для «найди/есть ли» используй search_repositories или search_code; "
            "get_file_contents только когда путь уже известен. "
            "Найдя 2-3 результата, сразу дай итоговый ответ."''')])

# 3) .env: больше шагов файловому агенту
p = ".env"
lines = open(p, encoding="utf-8").read().splitlines() if os.path.exists(p) else []
for k, v in (("AGENTICSEEK_FILE_STEPS", "30"),):
    for i, l in enumerate(lines):
        if re.match(rf"\s*{k}\s*=", l):
            lines[i] = f"{k}={v}"; break
    else:
        lines.append(f"{k}={v}")
open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("✅ .env: AGENTICSEEK_FILE_STEPS=15")

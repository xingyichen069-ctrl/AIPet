"""One management entry; business logic remains in the existing programs."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

# Imported by filename as well as executed directly; never import src/tools.py.
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import _environment as environment

ROOT = TOOLS.parent


@dataclass(frozen=True)
class Command:
    path: str
    title: str
    group: str
    prefix: tuple[str, ...] = ()
    defaults: tuple[str, ...] = ()
    usage: str = ''


COMMANDS = {
    'start': Command('tools/first_run.py', '启动桌宠', '日常使用'),
    'config': Command('tools/settings.py', '打开图形配置', '日常使用'),
    'status': Command('tools/status.py', '查看本机状态', '日常使用', defaults=('--log', '10'), usage='[--watch [秒]] [--log [行数]]'),
    'stop': Command('tools/pet_ctl.py', '停止桌宠', '日常使用', prefix=('stop',)),
    'diagnose': Command('tools/first_run.py', '在控制台启动桌宠，保留诊断输出', '日常使用', prefix=('diagnose',)),
    'memory': Command('src/memory.py', '查看或管理记忆', '日常使用', defaults=('show',), usage='[show|stats|search 关键词|其他原有子命令]'),
    'memory-view': Command('src/render.py', '生成并打开记忆面板', '日常使用', prefix=('--open',)),
    'qq': Command('tools/qq_ctl.py', '调用原有连接管理入口', '连接与平台', defaults=('status',), usage='[status|start|stop|restart|install|uninstall]'),
    'qq-panel': Command('tools/qq_panel.py', '配置 QQ 指令面板；list/apply 会调用平台接口', '连接与平台', usage='[preview|list|apply]'),
    'backup': Command('src/backup.py', '备份本机资料；恢复默认导出比较，凭据文件另存', '迁移维护', defaults=('list',), usage='[list|create [名称]|restore 备份名 [--in-place]|auto]'),
    'knowledge': Command('src/knowledge.py', '查看、建立或搜索本地知识库', '迁移维护', defaults=('stats',), usage='[stats|build|search 关键词]'),
    'thinking': Command('src/thinking.py', '查看或切换思考档位', '日常使用', usage='[list|set 档位|params|auto 问题]'),
    'consolidate': Command('src/consolidate.py', '查看记忆整理状态与待整理素材', '迁移维护', defaults=('status',), usage='[status|brief|done]'),
    'chat-check': Command('tools/test_typing.py', '在临时副本检查当前聊天界面', '开发检查'),
    'migrate': Command('tools/migrate.py', '迁移旧安装的私人资料', '迁移维护', usage='旧目录 [--dry-run] [--into 目标目录] [原有参数]'),
    'persona-migrate': Command('tools/migrate_persona_runtime.py', '预览人格格式迁移；--apply 才写入', '迁移维护', usage='安装目录 [--apply]'),
    'test': Command('tools/test_core.py', '在公开样例副本中运行离线回归', '开发检查', usage='[--loopback] [test_module ...]'),
    'tone-demo': Command('tools/tone_test.py', '预览语气探针；--live 才在临时副本调用 API', '开发实验', usage='[心情名称] [--live]'),
    'persona-probe': Command('tools/probe_reimu_rewrite.py', '人格改写试验；--live 才调用 API', '开发实验', usage='[--live]'),
}


def help_text() -> str:
    lines = ['小日和 · 项目管理', '', '用法：python tools/manage.py 命令 [参数]',
             '',
             '环境准备', '  prepare          准备项目专用环境；已有配置保持原样']
    groups = dict.fromkeys(command.group for command in COMMANDS.values())
    for group in groups:
        lines.extend(('', group))
        lines.extend(f'  {name:<16} {command.title}' for name, command in COMMANDS.items() if command.group == group)
    lines.extend(('', '旧的 tools/*.py 路径继续有效；不常用 BAT 已移入 tools/maintenance_维护入口。',
                  '查看单个入口：python tools/manage.py 命令 --help',
                  '目录与完整用法：docs/developer_开发者/README.md'))
    return '\n'.join(lines)


def run_command(name: str, args: list[str], root: Path = ROOT) -> int:
    # Help must be side-effect free, including legacy experiments whose module
    # bodies run immediately when imported. Do not import business modules here.
    if '-h' in args or '--help' in args:
        if name == 'prepare':
            print('prepare：准备项目环境。start 与 diagnose 也会检查并补齐缺失依赖。')
        else:
            command = COMMANDS[name]
            print(f'{name}：{command.title}\n用法：python tools/manage.py {name} {command.usage}\n实现：{command.path}')
        return 0
    if name in ('prepare', 'start', 'diagnose'):
        if args:
            raise ValueError(name + ' 不接受附加参数。')
        action = {'start': 'launch', 'prepare': 'prepare', 'diagnose': 'diagnose'}[name]
        return subprocess.run([sys.executable, '-X', 'utf8', str(root / 'tools/first_run.py'), action],
                              cwd=root, env=environment.command_env(root)).returncode
    command = COMMANDS[name]
    interpreter = environment.require_python(root)
    forwarded = list(command.prefix) + (args or list(command.defaults))
    return subprocess.run([str(interpreter), '-X', 'utf8', str(root / command.path), *forwarded],
                          cwd=root, env=environment.command_env(root)).returncode


def main(argv=None) -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=help_text(), formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', nargs='?', choices=['prepare', *COMMANDS], help='要执行的管理命令')
    parser.add_argument('args', nargs=argparse.REMAINDER, help='原样传给现有工具的参数')
    options = parser.parse_args(argv)
    if options.command is None:
        print(help_text())
        return 0
    try:
        return run_command(options.command, options.args, root=ROOT)
    except (OSError, ValueError, RuntimeError) as error:
        print('未完成：' + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())

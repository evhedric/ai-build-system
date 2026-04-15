from execution_wrapper import execute_command

# Example usage
commands = [
    'python -m pytest tests/',
    'python -m pip install requests'
]

for cmd in commands:
    execute_command(cmd)
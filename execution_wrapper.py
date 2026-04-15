import logging
from validate_plan import validate_plan

logging.basicConfig(level=logging.INFO)

def execute_command(command):
    if validate_plan(command):
        logging.info(f'Command validated: {command}')
        # Here you would normally execute the command
        # e.g., subprocess.run(command, shell=True)
        logging.info(f'Executing command: {command}')
    else:
        logging.error(f'Command validation failed: {command}')
"""Create the first local admin without exposing a remote setup route.

Windows: CREATE_ADMIN_ACCOUNT.bat
Docker:  docker compose exec api python -m app.bootstrap_cli
"""
import getpass
from . import main


def main_cli():
    with main.auth_connection() as con:
        if con.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
            print('Первый аккаунт уже создан. Новых сотрудников добавляй через Профиль -> Команда.')
            return
    print('Первоначальная настройка администратора Grand Support Copilot')
    username = input('Логин (латиница, 3-32 символа): ').strip()
    full_name = input('Отображаемое имя: ').strip()
    password = getpass.getpass('Пароль (не менее 12 символов): ')
    confirm = getpass.getpass('Повтори пароль: ')
    if password != confirm:
        print('Пароли не совпали. Аккаунт не создан.')
        return
    try:
        data = main.AccountSetup(username=username, full_name=full_name, password=password)
    except Exception as error:
        print('Проверь введённые данные:', error)
        return
    with main.auth_connection() as con:
        con.execute('BEGIN IMMEDIATE')
        if con.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
            print('Администратор уже существует.')
            return
        main.insert_user(con, data)
        con.commit()
    print('Администратор создан. Открой интерфейс и войди с этим логином и паролем.')


if __name__ == '__main__':
    main_cli()

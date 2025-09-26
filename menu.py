import os
import sys
import json
import signal
import random
import asyncio
from typing import Optional

from colorama import init
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from InquirerPy import inquirer

from db.tradeDB import TradeSQL
from db.manager import AsyncDatabaseManager

from utils.session import session_manager
from utils.cookies import (
    save_cookies_to_account,
    check_cookies_from_account,
    apply_cookies_from_db
)
from utils.captcha import TwoCaptcha
from src.account.login import ArkhamLogin
from src.trade.trading_client import ArkhamTrading
from utils.leverage import ArkhamLeverage
from utils.size_calc import PositionSizer

from account import Account
from data import config


# --- Инициализация окружения ---
init(autoreset=True)
console = Console()

# --- Глобальные переменные ---
current_account: Optional[Account] = None
shutdown_event = asyncio.Event()
db: Optional[AsyncDatabaseManager] = None
_shutdown_in_progress = False


# --- Вспомогательные функции ---
def _normalize_proxy(raw: str) -> Optional[str]:
    raw = (raw or '').strip()
    if not raw:
        return None

    if '@' in raw:
        return raw

    parts = raw.split()
    if len(parts) == 2 and parts[0].startswith('http'):
        return f"{parts[0]}@{parts[1]}"

    if len(parts) == 2:
        return f"http://{parts[0]}@{parts[1]}"

    return raw


def db_row_to_account(row: dict) -> Account:
    """Преобразовать строку из БД в объект Account"""
    cookies = row.get("cookies")
    if cookies and isinstance(cookies, str):
        try:
            cookies = json.loads(cookies)
        except (json.JSONDecodeError, TypeError):
            pass

    return Account(
        account=row.get("account"),
        email=row.get("email"),
        password=row.get("password"),
        api_key=row.get("api_key"),
        api_secret=row.get("api_secret"),
        proxy=row.get("proxy"),
        cookies=cookies,
        captcha_api=row.get("captcha_api")
    )


# --- Завершение работы и обработчики ---
async def graceful_shutdown():
    global _shutdown_in_progress, db, current_account

    if _shutdown_in_progress:
        return
    _shutdown_in_progress = True

    console.print("\n[yellow]🔄 Завершение работы программы...[/yellow]")
    shutdown_event.set()

    try:
        if current_account:
            try:
                await current_account.close_session()
                console.print("[green]✅ Сессия аккаунта закрыта[/green]")
            except Exception as e:
                console.print(f"[yellow]⚠️ Ошибка закрытия сессии: {e}[/yellow]")

        try:
            await session_manager.close_all()
        except Exception as e:
            console.print(f"[yellow]⚠️ Ошибка закрытия сессий: {e}[/yellow]")

        if db:
            try:
                await db.close()
                console.print("[green]✅ База данных закрыта[/green]")
            except Exception as e:
                console.print(f"[yellow]⚠️ Ошибка закрытия БД: {e}[/yellow]")

        current_task = asyncio.current_task()
        tasks = [
            task for task in asyncio.all_tasks()
            if task != current_task and not task.done()
        ]

        if tasks:
            console.print(f"[yellow]🔄 Отменяем {len(tasks)} задач...[/yellow]")
            for task in tasks:
                task.cancel()

            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=3.0
                )
            except asyncio.TimeoutError:
                console.print("[yellow]⚠️ Некоторые задачи не завершились по таймауту[/yellow]")

        console.print("[green]✅ Программа корректно завершена[/green]")

    except Exception as e:
        console.print(f"[red]❌ Ошибка при завершении: {e}[/red]")

    finally:
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.stop()
        except:
            pass


def setup_interrupt_handler():
    """Настройка обработчика прерывания"""
    def signal_handler(sig, frame):
        console.print(f"\n[yellow]⚠️ Получен сигнал {sig}, завершение программы...[/yellow]")

        if _shutdown_in_progress:
            console.print("[red]⚠️ Принудительное завершение...[/red]")
            os._exit(1)

        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(graceful_shutdown())
            else:
                os._exit(0)
        except RuntimeError:
            os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, signal_handler)


# --- Меню ---
async def start_menu() -> Optional[Account]:
    """Меню выбора или добавления аккаунта"""
    while not shutdown_event.is_set():
        try:
            console.print(config.banner, justify="center")
            console.print("Для начала нужно выбрать или добавить аккаунт в базу данных", justify="center")

            choice = await inquirer.select(
                message='Выберите действие',
                choices=[
                    "✏️ Добавить аккаунт в ручную",
                    "👆 Выбрать аккаунт из БД",
                    "❌ Выход"
                ],
                default="👆 Выбрать аккаунт из БД",
            ).execute_async()

            match choice:
                case "✏️ Добавить аккаунт в ручную":
                    acc = await add_account()
                    if acc:
                        return acc
                case "👆 Выбрать аккаунт из БД":
                    acc = await select_account()
                    if acc:
                        return acc
                case "❌ Выход":
                    return None

        except KeyboardInterrupt:
            return None
        except Exception as e:
            if shutdown_event.is_set():
                return None
            console.print(f"[red]❌ Ошибка в стартовом меню: {e}[/red]")
            await asyncio.sleep(1)

    return None

async def main_menu(account: Account):
    """Главное меню с основной функциональностью"""
    global current_account
    current_account = account

    while not shutdown_event.is_set():
        try:
            console.print(
                f"\n[bold blue]🚀 ARKHAM TRADING SYSTEM[/bold blue] — [green]{current_account.account}[/green]",
                justify="center"
            )

            choice = await inquirer.select(
                message="Выберите действие:",
                choices=[
                    "📂 Управление базой данных",
                    "💹 Торговые операции",
                    "📊 Информация об аккаунте",
                    "❌ Выход",
                ],
                default="📂 Управление базой данных",
            ).execute_async()

            match choice:
                case "📂 Управление базой данных":
                    result = await database_menu(current_account)
                    
                    if result is None:
                        console.print("[yellow]⚠️ Завершение работы программы...[/yellow]")
                        return
                    elif isinstance(result, Account) and result.account != current_account.account:
                        await current_account.close_session()  
                        current_account = result
                        console.print(f"[green]✅ Переключились на аккаунт: {current_account.account}[/green]")

                case "💹 Торговые операции":
                    await trading_menu(current_account)

                case "📊 Информация об аккаунте":
                    await show_basic_account_info(current_account)
                case "❌ Выход":
                    return

        except KeyboardInterrupt:
            return
        except Exception as e:
            if shutdown_event.is_set():
                break
            console.print(f"[red]❌ Ошибка в главном меню: {e}[/red]")
            await asyncio.sleep(1)

# --- Подменю ---
async def database_menu(account: Account):
    """Меню управления базой данных"""
    while not shutdown_event.is_set():
        try:
            console.print("[yellow]📂 Меню управления базой данных[/yellow]", justify='center')

            choice = await inquirer.select(
                message="Выберите действие:",
                choices=[
                    "🗑️ Очистить таблицу",
                    "❌ Удалить конкретный аккаунт", 
                    "📋 Показать все аккаунты",
                    "⬅️ Назад"
                ],
                default="📋 Показать все аккаунты"
            ).execute_async()

            match choice:
                case "🗑️ Очистить таблицу":
                    await clear_table_action()
                    
                case "❌ Удалить конкретный аккаунт":
                    console.print("[yellow]⚠️ Внимание: Если у вас один аккаунт, то при удалении программа завершится![/yellow]")
                    result = await delete_account_action(account)
                    if result == "account_deleted":
                        console.print("[yellow]⚠️ Текущий аккаунт был удален. Необходимо выбрать другой аккаунт.[/yellow]")
                        new_account = await select_account()
                        if new_account:
                            console.print(f"[green]✅ Выбран новый аккаунт: {new_account.account}[/green]")
                            return new_account 
                        else:
                            console.print("[red]❌ Аккаунт не выбран. Завершение работы.[/red]")
                            return None
                    elif result == "other_deleted":
                        continue
                    elif result == "cancelled":
                        continue
                        
                case "📋 Показать все аккаунты":
                    await show_all_accounts()
                    
                case "⬅️ Назад":
                    return account  

        except KeyboardInterrupt:
            return account
        except Exception as e:
            if not shutdown_event.is_set():
                console.print(f"[red]❌ Ошибка в меню БД: {e}[/red]")
                await asyncio.sleep(1)

    return account

async def show_basic_account_info(account: Account):
    """Показать базовую информацию об аккаунте"""
    try:
        console.print("[blue]📊 Загрузка информации об аккаунте...[/blue]")

        if not account.arkham_info:
            await account.initialize_clients()

        balance = await account.arkham_info.get_balance()
        points = await account.arkham_info.get_volume_or_points('points')
        volume = await account.arkham_info.get_volume_or_points('volume')

        if shutdown_event.is_set():
            return

        table = Table(title=f"📊 Основная информация: {account.account}")
        table.add_column("Параметр", style="cyan", width=25)
        table.add_column("Значение", style="green", width=20)

        table.add_row("💰 Баланс", f"${balance:.2f}")
        table.add_row("🏆 Очки", str(points))
        table.add_row("📈 Объем торгов", f"${volume:.2f}")
        table.add_row("💸 Маржа для комиссий", f"${account.margin_fee:.2f}")
        table.add_row("🎁 Маржа бонус", f"${account.margin_bonus:.2f}")

        console.print(table)

        if not shutdown_event.is_set():
            await inquirer.text(message="Нажмите Enter для продолжения...").execute_async()

    except Exception as e:
        if not shutdown_event.is_set():
            console.print(f"[red]❌ Ошибка получения базовой информации: {e}[/red]")
            await asyncio.sleep(2)


async def trading_menu(account: Account):
    while True:
        await account.initialize_clients()

        choice = await inquirer.select(
            message='Выберите тип торговли',
            choices=[
                '👛 Futures trading',
                '🎒 Spot trading',
                '⬅️ Выйти',
            ],
            default='Futures trading'
        ).execute_async()

        match choice:
            case '👛 Futures trading':
                await futures_menu(account)

            case '🎒 Spot trading':
                await spot_menu(account)

            case '⬅️ Выйти':
                break

async def futures_menu(account: Account):
    while True:
        choice = await inquirer.select(
            message="Выберите действие:",
            choices=[
                "📋 Мои позиции",
                "📈 Открыть LONG",
                "📉 Открыть SHORT",
                "❌ Закрыть все позиции",
                "⬅️ Выйти",
            ],
                default="📋 Мои позиции"
        ).execute_async()

        match choice:
            case "📋 Мои позиции":
                await positions_and_balances_menu(account)

            case "📈 Открыть LONG":
                await open_position(account, side="long")

            case "📉 Открыть SHORT":
                await open_position(account, side="short")

            case "❌ Закрыть все позиции":
                await close_all_positions(account)

            case "⬅️ Выйти":
                break

async def spot_menu(account: Account):
    while True:
        choice = await inquirer.select(
            message="Выберите действие:",
            choices=[
                "📋 Баланс монет на споте",
                "📈 Купить монету",
                "📉 Продать монету",
                "⬅️ Выйти",
            ],
                default="📋 Мои позиции"
        ).execute_async()

        match choice:
            case "📋 Баланс монет на споте":
                await spot_balance(account)

            case "📈 Купить монету":
                await spot_buy_coin(account)

            case "📉 Продать монету":
                await spot_sell_coin(account)

            case "⬅️ Выйти":
                break

async def positions_and_balances_menu(account: Account):
    """Меню: просмотр и закрытие позиций"""
    try:
        await account.initialize_clients()
        positions = await account.arkham_info.get_all_positions()

        if not positions:
            console.print("[yellow]⚠️ У вас нет открытых позиций[/yellow]")
            await asyncio.sleep(2)
            return

        # Показываем таблицу
        table = Table(title="📋 Мои позиции")
        table.add_column("Монета", style="cyan")
        table.add_column("Размер", style="green")
        table.add_column("Направление", style="blue")
        table.add_column("Маржа", style="yellow")
        table.add_column("PnL", style="magenta")
        table.add_column("Entry", style="gray")


        for coin, pos in positions.items():
            size = pos.get("base", 0)
            direction = "LONG" if float(size) > 0 else "SHORT"
            table.add_row(
                coin,
                str(size),
                direction,
                str(pos.get("value", "N/A")),
                str(pos.get("pnl", "N/A")),
                str(pos.get('entry', "N/A"))
            )

        console.print(table)

        # Выбор действия
        choice = await inquirer.select(
            message="Выберите позицию для закрытия или вернитесь назад:",
            choices=list(positions.keys()) + ["⬅️ Назад"]
        ).execute_async()

        if choice == "⬅️ Назад":
            return

        # Закрываем выбранную позицию
        position = positions[choice]
        size = abs(position["base"])
        direction = "LONG" if float(position["base"]) > 0 else "SHORT"

        trader = ArkhamTrading(
            session=account.session,
            coin=choice,
            size=size,
            info_client=account.arkham_info
        )

        console.print(f"[blue]📊 Закрываем {direction} по {choice} на {size}[/blue]")

        if direction == "LONG":
            success = await trader.futures_close_long_market(position_size=size)
        else:
            success = await trader.futures_close_short_market(position_size=size)

        if success:
            console.print(f"[green]✅ Позиция {choice} закрыта![/green]")
        else:
            console.print(f"[red]❌ Ошибка закрытия позиции {choice}[/red]")

        await asyncio.sleep(2)

    except Exception as e:
        console.print(f"[red]❌ Ошибка в меню позиций: {e}[/red]")
        await asyncio.sleep(2)
    
async def trading_dialog_window(account: Account):
    while True:
        choice = await inquirer.select(
            choices=[
                "Открыть одну сделку",
                "Открыть несколько сделок",
                "⬅️ Выйти",
            ],
                default="Открыть одну сделку"
        ).execute_async()

        match choice:
            case "Открыть одну сделку":
                await open_position(account)

            case "Открыть несколько сделок":
                await dialog_window(account)

            case "📉 Продать монету":
                await spot_sell_coin(account)

            case "⬅️ Выйти":
                break

async def dialog_window(account: Account):
    try:
        while True:
            choice = await inquirer.select(
                message='Как отсчитывать сделки?',
                choices=[
                    "По строгому количеству (задаете сами)",
                    "По остатку баланса (торгуется до заданого)",
                    "⬅️ Выйти",
                ],
                    default="По строгому количеству (задаете сами)"
            ).execute_async()

            match choice:
                case "По строгому количеству (задаете сами)":
                    await multiple_orders(account)
                case "По остатку баланса (торгуется до заданого)":
                    await remains_balance_orders(account)
                case "⬅️ Выйти":
                    break
    except Exception as e:
        raise ValueError(e)

    

async def open_position(account: Account, side: str):
    coin = str(await inquirer.text(message="Введите монету (например BTC):").execute_async())

    # получаем цену
    price = dict(await account.arkham_price.get_futures_price(coin))['price']
    if not price:
        console.print(f"[red]❌ Не удалось получить цену {coin}[/red]")
        return

    # спрашиваем % от депо
    percent = await inquirer.number(
        message="Какой процент от депозита использовать?",
    ).execute_async()

    leverage_raw = (await inquirer.text(message="Введите плечо для вашей сделки (1 - 20):").execute_async())
    try:
        leverage = await ArkhamLeverage(account.session).check_leverage(coin.upper(), int(leverage_raw))
    except (TypeError, ValueError):
        console.print(f'Не удалось поставить плечо... Используем дефолтное - {config.DEFAULT_LEVERAGE}')
        leverage = config.DEFAULT_LEVERAGE

    # рассчитываем размер позиции
    size = PositionSizer(account.balance, int(leverage), float(price), float(percent)).calculate_size()
    trader = ArkhamTrading(
        session=account.session,
        coin=coin,
        size=size,
        info_client=account.arkham_info
    )

    if side == "long":
        success = await trader.futures_long_market()
    else:
        success = await trader.futures_short_market()

    if success:
        console.print(f"[green]✅ {side.upper()} по {coin} открыт[/green]")
    else:
        console.print(f"[red]❌ Ошибка открытия позиции[/red]")

async def close_all_positions(account: Account):
    trader = ArkhamTrading(
        session=account.session,
        coin='LOLKEK',       # Все нормально, так нужно!!!
        size='2 бутерброда', # Все нормально, так нужно!!!
        info_client=account.arkham_info)
    results = await trader.futures_close_position_market()
    if results:
        console.print(f"[green]✅ Закрыты все позиции: {list(results.keys())}[/green]")
    else:
        console.print("[yellow]⚠️ Нет открытых позиций[/yellow]")
    await asyncio.sleep(2)

async def spot_balance(account: Account):
    try:
        balances = await account.arkham_info.get_spot_balances()
        if not balances:
            console.print("[yellow]⚠️ На спотовом кошельке нет активов[/yellow]")
            return

        table = Table(title="💰 Спотовый баланс")
        table.add_column("Монета", style="cyan")
        table.add_column("Количество", style="green")
        table.add_column("Цена в USDT", style='blue')

        for item in balances:
            table.add_row(item['symbol'], str(item['free']), item['balanceUSDT'])

        console.print(table)

        await inquirer.text(message="Нажмите Enter для продолжения...").execute_async()

    except Exception as e:
        console.print(f"[red]❌ Ошибка получения спотового баланса: {e}[/red]")

async def spot_buy_coin(account: Account):
    coin = str(await inquirer.text(message="Введите монету (например BTC):").execute_async())

    # получаем цену
    price = dict(await account.arkham_price.get_spot_price(coin))['price']
    if not price:
        console.print(f"[red]❌ Не удалось получить цену {coin}[/red]")
        return

    percent = await inquirer.number(
        message="Какой процент от депозита использовать?",
    ).execute_async()

    size = PositionSizer(
        account.balance, 
        1, 
        float(price), 
        int(percent)
    ).calculate_size()

    trader = ArkhamTrading(
        session=account.session,
        coin=coin.upper(),
        size=size,
        price=price,
        info_client=account.arkham_info
    )
    success = await trader.spot_buy_market()  
    if success:
        console.print(f"[green]✅ Монета '{coin.upper()}'' куплена на споте![/green]")
    else:
        console.print(f"[red]❌ Ошибка покупки позиции[/red]")

async def spot_sell_coin(account: Account):
    try:
        coins = await account.arkham_info.get_spot_balances()
        choices = [coin['symbol'] for coin in coins]
    except Exception as e:
        console.print(f'[red]Ошибка получения балансов: {e}[/red]')
        return False

    try:
        choice = await inquirer.select(
            message='Выберите монету для продажи',
            choices=choices + ["❌ Отмена"],
        ).execute_async()

        if choice == "❌ Отмена":
            return None

        try:
            size = float(next(coin['free'] for coin in coins if coin['symbol'] == choice))
        except StopIteration:
            console.print(f"[red]❌ Баланс для {choice} не найден[/red]")
            return False

        price_data = await account.arkham_price.get_spot_price(choice)
        price = price_data.get('price')
        if not price:
            console.print(f"[red]❌ Не удалось получить цену {choice}[/red]")
            return False

        trader = ArkhamTrading(
            session=account.session,
            coin=choice,
            size=size,
            price=price,
            info_client=account.arkham_info
        )

        success = await trader.spot_sell_market(sell_size=float(size))
        if success:
            console.print(f"[green]✅ Монета '{choice}' продана на споте![/green]")
        else:
            console.print(f"[red]❌ Ошибка продажи позиции[/red]")

    except Exception as e:
        console.print(f"[red]Ошибка при продаже монеты: {e}[/red]")
        return False

# --- Работа с аккаунтами и БД ---
async def clear_table_action():
    """Очистка таблицы с подтверждением"""
    try:
        confirmation = await inquirer.select(
            message="⚠️ Вы уверены, что хотите очистить всю таблицу? Это действие необратимо!",
            choices=["❌ НЕТ, отмена", "✅ ДА, очистить"],
            default="❌ НЕТ, отмена"
        ).execute_async()
        
        if confirmation == "✅ ДА, очистить":
            trade_table = TradeSQL(db)
            await trade_table.clear_table(config.TABLE_NAME)
            console.print("[green]✅ Таблица очищена[/green]")
        else:
            console.print("[yellow]⚠️ Очистка отменена[/yellow]")
            
    except Exception as e:
        console.print(f"[red]❌ Ошибка очистки таблицы: {e}[/red]")

async def delete_account_action(current_account: Account) -> str:
    """
    Удаление конкретного аккаунта
    
    Returns:
        "account_deleted" - если удален текущий аккаунт
        "other_deleted" - если удален другой аккаунт  
        "cancelled" - если операция отменена
    """
    try:
        trade_table = TradeSQL(db)
        accounts = await trade_table.get_all(config.TABLE_NAME)
        
        if not accounts:
            console.print("[red]❌ Нет аккаунтов в базе данных[/red]")
            return "cancelled"
            
        account_names = [acc.get("account", "Unknown") for acc in accounts]
        
        selected_name = await inquirer.select(
            message="Выберите аккаунт для удаления:",
            choices=account_names + ["❌ Отмена"]
        ).execute_async()
        
        if selected_name == "❌ Отмена" or shutdown_event.is_set():
            return "cancelled"
            
        # Подтверждение удаления
        confirmation = await inquirer.select(
            message=f"⚠️ Удалить аккаунт '{selected_name}'? Это действие необратимо!",
            choices=["❌ НЕТ, отмена", "✅ ДА, удалить"],
            default="❌ НЕТ, отмена"
        ).execute_async()
        
        if confirmation == "✅ ДА, удалить":
            success = await trade_table.delete_account(config.TABLE_NAME, selected_name)
            
            if success:
                console.print(f"[green]✅ Аккаунт '{selected_name}' успешно удален[/green]")
                
                # Проверяем, удален ли текущий активный аккаунт
                if selected_name == current_account.account:
                    return "account_deleted"
                else:
                    return "other_deleted"
            else:
                console.print(f"[red]❌ Не удалось удалить аккаунт '{selected_name}'[/red]")
                return "cancelled"
        else:
            console.print("[yellow]⚠️ Удаление отменено[/yellow]")
            return "cancelled"
            
    except Exception as e:
        console.print(f"[red]❌ Ошибка удаления аккаунта: {e}[/red]")
        return "cancelled"


async def show_all_accounts():
    """Показать все аккаунты в виде таблицы"""
    try:
        trade_table = TradeSQL(db)
        accounts = await trade_table.get_all(config.TABLE_NAME)
        
        if not accounts:
            console.print("[red]❌ Нет аккаунтов в базе данных[/red]")
            return
            
        table = Table(title="📋 Все аккаунты в базе данных")
        table.add_column("Аккаунт", style="cyan")
        table.add_column("Email", style="green") 
        table.add_column("Баланс", style="yellow")
        table.add_column("Объем", style="blue")
        table.add_column("Очки", style="magenta")
        
        for acc in accounts:
            table.add_row(
                str(acc.get("account", "N/A")),
                str(acc.get("email", "N/A")), 
                str(acc.get("balance", "N/A")),
                str(acc.get("volume", "N/A")),
                str(acc.get("points", "N/A"))
            )
            
        console.print(table)
        
        if not shutdown_event.is_set():
            await inquirer.text(message="Нажмите Enter для продолжения...").execute_async()
            
    except Exception as e:
        console.print(f"[red]❌ Ошибка получения списка аккаунтов: {e}[/red]")

async def select_account() -> Optional[Account]:
    """Выбор аккаунта из базы данных"""
    try:
        trade_table = TradeSQL(db)
        accounts = await trade_table.get_all(config.TABLE_NAME)

        if not accounts:
            console.print("[red]❌ Нет аккаунтов в базе данных[/red]")
            return None

        account_names = [acc.get("account", "Unknown") for acc in accounts]
        selected_name = await inquirer.select(
            message="Выберите аккаунт:",
            choices=account_names + ["❌ Отмена"]
        ).execute_async()

        if selected_name == "❌ Отмена" or shutdown_event.is_set():
            return None

        acc_data = await trade_table.get_account(config.TABLE_NAME, selected_name)
        account = db_row_to_account(acc_data)

        await account.create_session()

        cookies_loaded = await apply_cookies_from_db(account.session, db, config.TABLE_NAME, account.account)
        
        if cookies_loaded:
            console.print("[green]✅ Куки загружены из БД в сессию[/green]")
            
            cookies_from_db = await trade_table.get_cookies(config.TABLE_NAME, account.account)
            if cookies_from_db:
                account.cookies = cookies_from_db
            
            cookies_valid = await check_cookies_from_account(account)
            
            if cookies_valid:
                console.print("[green]✅ Куки валидны, обновляем данные аккаунта[/green]")
                await account.update_data()
                
                await trade_table.update_account_data(
                    config.TABLE_NAME,
                    account.account,
                    account.balance,
                    account.volume,
                    account.points,
                    account.margin_fee,
                    account.margin_bonus,
                    cookies=None  
                )
                return account
            else:
                console.print("[yellow]⚠️ Куки не валидны (старше 30 минут), требуется повторный логин[/yellow]")
        else:
            console.print("[yellow]⚠️ Куки в БД не найдены[/yellow]")

        console.print("[blue]🔐 Выполняется повторная авторизация...[/blue]")
        account = await login_arkham(account)
        
        if not account or shutdown_event.is_set():
            console.print("[red]❌ Авторизация не удалась[/red]")
            return None
            
        await account.update_data()
        
        await save_cookies_to_account(account.session, account)
        await trade_table.update_account_data(
            config.TABLE_NAME,
            account.account,
            account.balance,
            account.volume,
            account.points,
            account.margin_fee,
            account.margin_bonus,
            account.cookies
        ) 

        return account

    except Exception as e:
        if not shutdown_event.is_set():
            console.print(f"[red]❌ Ошибка выбора аккаунта: {e}[/red]")
        return None


async def add_account() -> Optional[Account]:
    """Добавление нового аккаунта"""
    try:
        account_name = await inquirer.text(message="Введите название аккаунта:").execute_async()
        email = await inquirer.text(message="Введите email:").execute_async()
        password = await inquirer.text(message="Введите пароль:").execute_async()
        raw_proxy = await inquirer.text(
            message="Введите прокси (например: http://user:pass ip:port или http://user:pass@ip:port):"
        ).execute_async()
        proxy = _normalize_proxy(str(raw_proxy)) 
        api_key = await inquirer.text(message="Введите Arkham api_key:").execute_async()
        api_secret = await inquirer.text(message="Введите Arkham api_secret:").execute_async()
        captcha_api = await inquirer.text(message="Введите TwoCaptcha captcha_key:").execute_async()
        
        account = Account(
            account=account_name,
            email=email,
            password=password,
            proxy=proxy or None,
            api_key=api_key,
            api_secret=api_secret,
            captcha_api=captcha_api
        )

        await account.create_session()
        
        if shutdown_event.is_set():
            await account.close_session()
            return None

        status = await account.session_check(db)
        if not status and not shutdown_event.is_set():
            choice = await inquirer.select(
                message="Пройти процесс авторизации?",
                choices=["☑️ Да", "❌ Выход"],
                default="☑️ Да",
            ).execute_async()

            if shutdown_event.is_set():
                await account.close_session()
                return None

            match choice:
                case "☑️ Да":
                    account = await login_arkham(account)
                    if not account or shutdown_event.is_set():
                        console.print("[red]❌ Авторизация не удалась[/red]")
                        return None 
                case "❌ Выход":
                    await account.close_session()
                    return None
        
        if shutdown_event.is_set():
            await account.close_session()
            return None
                
        # Инициализируем клиентов
        await account.initialize_clients()
        
        if shutdown_event.is_set():
            await account.close_session()
            return None
        
        balance = float(await account.arkham_info.get_balance())
        points = int(await account.arkham_info.get_volume_or_points('points'))
        volume = float(await account.arkham_info.get_volume_or_points('volume'))
        margin_bonus, margin_fee = await account.arkham_info.get_fee_margin()

        if shutdown_event.is_set():
            await account.close_session()
            return None
        
        await save_account_to_db(account, points, volume, balance, margin_fee, margin_bonus, captcha_api)
        
        return account
        
    except Exception as e:
        if not shutdown_event.is_set():
            console.print(f"[red]❌ Ошибка добавления аккаунта: {e}[/red]")
        return None


async def save_account_to_db(
        account: Account,
        points: Optional[int] | None = None,
        volume: Optional[float] | None = None,
        balance: Optional[float] | None = None,
        margin_fee: Optional[float] | None = None,
        margin_bonus: Optional[float] | None = None,
        captcha_api: Optional[float] | None = None,
    ):
    """Сохранить аккаунт в базу данных"""
    if shutdown_event.is_set():
        return
        
    try:
        trade_table = TradeSQL(db)
        account_data = account.model_dump(
            exclude={"arkham_info", "arkham_login", "arkam_price", "arkham_trader", "session", "_session_manager"}
        )
        
        if account_data.get('cookies'):
            if isinstance(account_data['cookies'], (dict, list, tuple)):
                account_data['cookies'] = json.dumps(account_data['cookies'])
            elif not isinstance(account_data['cookies'], str):
                account_data['cookies'] = str(account_data['cookies'])
        
        # Добавляем статистику
        account_data.update({
            "balance": balance,
            "points": points,
            "volume": volume,
            "margin_fee": margin_fee,
            "margin_bonus": margin_bonus,
        })
        
        # Обрабатываем остальные поля
        for key, value in account_data.items():
            if isinstance(value, (tuple, list)) and key != 'cookies':
                account_data[key] = json.dumps(value)
            elif value is None:
                account_data[key] = None
            elif not isinstance(value, (str, int, float, bytes)):
                account_data[key] = str(value)
        
        await trade_table.add_info(config.TABLE_NAME, account_data)
        console.print(f"[green]✅ Аккаунт '{account.account}' успешно добавлен в базу[/green]")
        
    except Exception as e:
        if not shutdown_event.is_set():
            console.print(f"[red]❌ Ошибка сохранения аккаунта в БД: {e}[/red]")


async def login_arkham(account: Account) -> Optional[Account]:
    """Авторизация в Arkham"""
    try:
        session = await account.ensure_session()

        captcha = TwoCaptcha(session, account.captcha_api)
        token = await captcha.solve_turnstile()

        arkham_login = ArkhamLogin(
            session=session,
            password=account.password,
            email=account.email,
            turnstile_token=token,
        )

        status = await arkham_login.login_arkham()
        if not status or shutdown_event.is_set():
            console.print("[red]❌ Ошибка: не удалось войти в Arkham[/red]")
            return None

        code_2fa = await arkham_login.input_2fa()
        if shutdown_event.is_set():
            return None
            
        ok = await arkham_login.verify_2FA(code_2fa)
        if not ok or shutdown_event.is_set():
            console.print("[red]❌ Ошибка: неверный код 2FA[/red]")
            return None

        updated_account = await save_cookies_to_account(session, account)
        console.print("[green]✅ Успешный логин, куки сохранены в Account[/green]")

        return updated_account

    except Exception as e:
        if not shutdown_event.is_set():
            import traceback
            console.print(f"[red]❌ Ошибка login: {repr(e)}[/red]")
            console.print(traceback.format_exc())
        return None


    
async def create_table():
    """Создание таблицы если её нет"""
    try:
        trade_table = TradeSQL(db)
        await trade_table.create_table(config.TABLE_NAME)
        console.print("[green]✅ Таблица успешно создана/проверена[/green]")
    except Exception as e:
        if not shutdown_event.is_set():
            console.print(f"[red]❌ Ошибка создания таблицы: {e}[/red]")

# --- Main ---
async def main():
    """Главная функция программы с улучшенной обработкой завершения"""
    global db 
    try:
        setup_interrupt_handler()
        
        console.print(Panel.fit(
            "[bold blue]🚀 ARKHAM TRADING SYSTEM[/bold blue]\n"
            "[yellow]Нажмите Ctrl+C для корректного завершения[/yellow]",
            title="Запуск системы"
        ))

        db = AsyncDatabaseManager(config.DB_NAME)

        await create_table()
        
        if shutdown_event.is_set():
            return

        # Выбираем аккаунт
        account = await start_menu()
        if not account or shutdown_event.is_set():
            console.print("[yellow]⚠️ Аккаунт не выбран или программа прервана[/yellow]")
            return

        console.print(f"[green]✅ Работаете с аккаунтом: {account.account}[/green]")

        # Запускаем главное меню
        await main_menu(account)

    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️ Программа прервана пользователем[/yellow]")
    except Exception as e:
        console.print(f"[red]❌ Критическая ошибка: {e}[/red]")
    finally:
        # Корректное завершение
        if not _shutdown_in_progress:
            await graceful_shutdown()


if __name__ == "__main__":
    try:
        if hasattr(asyncio, 'WindowsProactorEventLoopPolicy') and os.name == 'nt':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            loop.run_until_complete(main())
        finally:
            pending = asyncio.all_tasks(loop)
            if pending:
                for task in pending:
                    task.cancel()
                
                try:
                    loop.run_until_complete(
                        asyncio.wait_for(
                            asyncio.gather(*pending, return_exceptions=True),
                            timeout=2.0
                        )
                    )
                except asyncio.TimeoutError:
                    pass
            
            loop.close()
            
    except KeyboardInterrupt:
        print("\nПрограмма завершена пользователем")
    except Exception as e:
        print(f"Ошибка запуска: {e}")
    finally:
        print("Выход из программы...")
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

from config.sql_statements_sqlite import CREATE_TABLE_DEFAULT, CREATE_INDEXES, INSERT_DATA
from scripts.utils import load_config, get_base_path, get_output_path

config = load_config()

# 配置日志记录
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

IS_SCRIPT_RUN = True

def get_base_path():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if IS_SCRIPT_RUN else os.getcwd()

# 定义读取分类信息的函数
def load_categories(config_path=None):
    if config_path is None:
        base_path = get_base_path()
        config_path = os.path.join(base_path, 'config', 'categories.json')
    """从 categories.json 文件中加载分类信息"""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            categories = json.load(f)
            duplicated_tags = set(categories.get('duplicated_tags', []))
            unique_tag_to_main = categories.get('unique_tag_to_main', {})
            logger.info(f"成功加载分类信息：{config_path}")
            return duplicated_tags, unique_tag_to_main
    except Exception as e:
        logger.error(f"加载分类信息时发生错误: {e}")
        return set(), {}

# 加载分类信息
duplicated_tags, unique_tag_to_main = load_categories()

class SnowflakeIDGenerator:
    def __init__(self, machine_id=1, datacenter_id=1):
        self.lock = threading.Lock()
        self.machine_id = machine_id & 0x3FF
        self.datacenter_id = datacenter_id & 0x3FF
        self.sequence = 0
        self.last_timestamp = -1
        self.epoch = 1609459200000

    def _current_millis(self):
        return int(time.time() * 1000)

    def get_id(self):
        with self.lock:
            timestamp = self._current_millis()

            if timestamp < self.last_timestamp:
                raise Exception("时钟向后移动。拒绝生成 id。")

            if timestamp == self.last_timestamp:
                self.sequence = (self.sequence + 1) & 0xFFF
                if self.sequence == 0:
                    while timestamp <= self.last_timestamp:
                        timestamp = self._current_millis()
            else:
                self.sequence = 0

            self.last_timestamp = timestamp

            id = ((timestamp - self.epoch) << 22) | (self.datacenter_id << 12) | self.sequence
            return id

id_generator = SnowflakeIDGenerator(machine_id=1, datacenter_id=1)

def get_years():
    current_year = datetime.now().year
    previous_year = current_year - 1
    return current_year, previous_year

def create_connection(db_file):
    """创建数据库连接"""
    try:
        os.makedirs(os.path.dirname(db_file), exist_ok=True)
        conn = sqlite3.connect(db_file)
        logger.info(f"成功连接到SQLite数据库: {db_file}")
        return conn
    except sqlite3.Error as e:
        logger.error(f"连接数据库时发生错误: {e}")
        return None

def table_exists(conn, table_name):
    """检查表是否存在"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT count(name) FROM sqlite_master
        WHERE type='table' AND name=?
    """, (table_name,))
    return cursor.fetchone()[0] > 0

def create_table(conn, table_name):
    """创建数据表"""
    cursor = conn.cursor()

    # 使用 sql_statements_sqlite.py 中的建表语句
    cursor.execute(CREATE_TABLE_DEFAULT.format(table=table_name))

    # 创建索引
    for index_sql in CREATE_INDEXES:
        cursor.execute(index_sql.format(table=table_name))

    conn.commit()
    logger.info(f"成功创建表 {table_name} 及其索引")

def batch_insert_data(conn, table_name, data_batch):
    """批量插入数据"""
    cursor = conn.cursor()

    # 使用 sql_statements_sqlite.py 中的插入语句
    placeholders = ','.join(['?' for _ in range(34)])  # 34个字段
    insert_sql = INSERT_DATA.format(table=table_name, placeholders=placeholders)

    try:
        cursor.executemany(insert_sql, data_batch)
        conn.commit()
        return len(data_batch)
    except sqlite3.Error as e:
        logger.error(f"插入数据时发生错误: {e}")
        conn.rollback()
        return 0

def get_last_import_time():
    """获取上次导入时间"""
    try:
        last_import_file = os.path.join(get_output_path(), 'last_import.json')
        if os.path.exists(last_import_file):
            with open(last_import_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('last_import_time', 0)
        logger.info("未找到last_import.json文件，将导入所有数据")
        return 0
    except Exception as e:
        logger.error(f"读取上次导入时间失败: {e}")
        return 0

def import_data_from_json(conn, table_name, file_path, last_import_time=0, batch_size=1000, sync_deleted=False):
    # Revisit existing events as their remote playback progress can change.
    from scripts.history_sync import import_records
    with open(file_path, 'r', encoding='utf-8-sig') as stream:
        records = json.load(stream)
    with conn:
        result = import_records(conn, records, timezone=None, sync_deleted=sync_deleted, id_factory=id_generator.get_id)
    return result['inserted']


def save_last_import_record(file_path, timestamp):
    """保存最后导入记录"""
    record = {
        "last_import_file": file_path,
        "last_import_time": timestamp,
        "last_import_date": datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    }

    record_file = get_output_path('last_import.json')
    with open(record_file, 'w', encoding='utf-8') as f:
        json.dump(record, f, ensure_ascii=False, indent=4)
    logger.debug(f"已更新导入记录: {record}")

def get_last_import_record():
    """获取最后导入记录"""
    record_file = get_output_path('last_import.json')
    if os.path.exists(record_file):
        with open(record_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def import_all_history_files(sync_deleted=False):
    """导入所有历史记录文件"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"========== 运行时间: {current_time} ==========")
    logger.info(f"当前工作目录: {os.getcwd()}")

    # 使用 get_output_path 获取路径
    full_data_folder = get_output_path('history_by_date')
    full_db_file = get_output_path(config['db_file'])

    logger.info(f"\n=== 路径信息 ===")
    logger.info(f"数据文件夹: {full_data_folder}")
    logger.info(f"数据库文件: {full_db_file}")

    if not os.path.exists(full_data_folder):
        message = f"本地文件夹 '{full_data_folder}' 不存在，无法加载数据。"
        logger.error(message)
        return {"status": "error", "message": message}

    # 获取最后导入记录
    last_record = get_last_import_record()
    last_import_time = last_record['last_import_time'] if last_record else 0

    if last_record:
        logger.info(f"上次导入记录:")
        logger.info(f"- 文件: {last_record['last_import_file']}")
        logger.info(f"- 时间: {last_import_time}")
        logger.info(f"- 日期: {last_record['last_import_date']}")
    else:
        logger.info("未找到导入记录，将导入所有数据")

    file_insert_counts = {}

    logger.info(f"开始遍历并导入文件夹 '{full_data_folder}' 中的数据...")

    conn = create_connection(full_db_file)
    if conn is None:
        message = f"无法连接到数据库 {full_db_file}。"
        logger.error(message)
        return {"status": "error", "message": message}

    try:
        # 遍历文件并导入
        total_files = 0
        total_records = 0
        latest_timestamp = 0  # 记录最新的时间戳
        latest_file = None  # 记录最新的文件

        # 获取所有JSON文件并按日期排序
        all_json_files = []
        for year in sorted(os.listdir(full_data_folder), reverse=True):  # 从最新的年份开始
            year_path = os.path.join(full_data_folder, year)
            if os.path.isdir(year_path) and year.isdigit():
                for month in sorted(os.listdir(year_path), reverse=True):  # 从最新的月份开始
                    month_path = os.path.join(year_path, month)
                    if os.path.isdir(month_path) and month.isdigit():
                        for day_file in sorted(os.listdir(month_path), reverse=True):  # 从最新的日期开始
                            if day_file.endswith('.json'):
                                day_path = os.path.join(month_path, day_file)
                                all_json_files.append(day_path)

        for day_path in all_json_files:
            logger.info(f"\n处理文件: {day_path}")

            # 读取文件中最新的记录时间
            try:
                with open(day_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if data:
                        newest_view_at = max(item.get('view_at', 0) for item in data)
                        logger.info(f"文件中最新记录时间: {datetime.fromtimestamp(newest_view_at)}")

                        # 更新最新的时间戳
                        if newest_view_at > latest_timestamp:
                            latest_timestamp = newest_view_at
                            latest_file = day_path

            except Exception as e:
                logger.error(f"读取文件 {day_path} 时出错: {e}")
                continue

            inserted_count = import_data_from_json(conn, "bilibili_history", day_path, last_import_time, sync_deleted=sync_deleted)
            if inserted_count > 0:
                total_files += 1
                total_records += inserted_count
                file_insert_counts[day_path] = inserted_count
                logger.info(f"成功插入 {inserted_count} 条记录")

        # 在所有文件处理完成后，使用最新的时间戳更新导入记录
        if total_records > 0 and latest_timestamp > 0:
            save_last_import_record(latest_file, latest_timestamp)
            logger.info(f"更新导入记录为最新时间戳: {datetime.fromtimestamp(latest_timestamp)}")

        # 打印导入统计
        logger.info("\n=== 导入统计 ===")
        logger.info(f"处理文件总数: {total_files}")
        logger.info(f"插入记录总数: {total_records}")
        if file_insert_counts:
            logger.info("\n各文件插入详情:")
            for file_path, count in file_insert_counts.items():
                logger.info(f"- {os.path.basename(file_path)}: {count} 条记录")
        else:
            logger.info("\n没有新记录需要插入")
        logger.info("================\n")

        message = f"数据导入完成，共插入 {total_records} 条记录。"
        return {"status": "success", "message": message, "inserted_count": total_records}

    except sqlite3.Error as e:
        error_msg = f"数据库错误: {str(e)}"
        logger.error(f"=== 错误 ===\n{error_msg}\n===========")
        return {"status": "error", "message": error_msg}
    finally:
        if conn:
            conn.close()

# 允许脚本独立运行
if __name__ == '__main__':
    result = import_all_history_files()
    if result["status"] == "success":
        print(result["message"])
    else:
        print(f"错误: {result['message']}")

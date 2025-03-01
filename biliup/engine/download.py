import asyncio
import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import Generator
from urllib.parse import urlparse

import requests
import stream_gears

from biliup.config import config

logger = logging.getLogger('biliup')


class DownloadBase:
    def __init__(self, fname, url, suffix=None, opt_args=None):
        self.danmaku = None
        self.room_title = None
        if opt_args is None:
            opt_args = []
        # 主播单独传参会覆盖全局设置。例如新增了一个全局的filename_prefix参数，在下面添加self.filename_prefix = config.get('filename_prefix'),
        # 即可通过self.filename_prefix在下载或者上传时候传递主播单独的设置参数用于调用（如果该主播有设置单独参数，将会优先使用单独参数；如无，则会优先你用全局参数。）
        self.fname = fname
        self.url = url
        self.suffix = suffix
        self.title = None
        self.live_cover_path = None
        self.downloader = config.get('downloader', 'stream-gears')
        # ffmpeg.exe -i  http://vfile1.grtn.cn/2018/1542/0254/3368/154202543368.ssm/154202543368.m3u8
        # -c copy -bsf:a aac_adtstoasc -movflags +faststart output.mp4
        self.raw_stream_url = None
        self.filename_prefix = config.get('filename_prefix')
        self.use_live_cover = config.get('use_live_cover', False)
        self.opt_args = opt_args
        # 是否是下载模式 跳过下播检测
        self.is_download = False
        self.live_cover_url = None
        self.fake_headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Accept-Language': 'zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.159 Safari/537.36',
        }

        self.default_output_args = [
            '-bsf:a', 'aac_adtstoasc',
        ]
        if config.get('segment_time'):
            self.default_output_args += \
                ['-to', f"{config.get('segment_time', '00:50:00')}"]
        else:
            self.default_output_args += \
                ['-fs', f"{config.get('file_size', '2621440000')}"]

    def check_stream(self, is_check=False):
        # is_check 是否是检测可以避免在检测是否可以录制的时候忽略一些耗时的操作
        logger.debug(self.fname, is_check)
        raise NotImplementedError()

    @staticmethod
    def batch_check(check_urls: list[str]) -> Generator[str, None, None]:
        # 批量检测直播或下载状态
        # 返回的是url_list
        raise NotImplementedError()

    def get_filename(self, is_fmt=False):
        if self.filename_prefix:  # 判断是否存在自定义录播命名设置
            filename = (self.filename_prefix.format(streamer=self.fname, title=self.room_title).encode(
                'unicode-escape').decode()).encode().decode("unicode-escape")
        else:
            filename = f'{self.fname}%Y-%m-%dT%H_%M_%S'
        filename = get_valid_filename(filename)
        if is_fmt:
            return time.strftime(filename.encode("unicode-escape").decode()).encode().decode("unicode-escape")
        else:
            return filename

    def download(self, filename):
        filename = self.get_filename()
        fmtname = time.strftime(filename.encode("unicode-escape").decode()).encode().decode("unicode-escape")

        threading.Thread(target=asyncio.run, args=(self.danmaku_download_start(fmtname),)).start()

        if self.downloader == 'streamlink':
            parsed_url = urlparse(self.raw_stream_url)
            path = parsed_url.path
            if '.flv' in path:  # streamlink无法处理flv,所以回退到ffmpeg
                return self.ffmpeg_download(fmtname)
            else:
                return self.streamlink_download(fmtname)
        elif self.downloader == 'ffmpeg':
            return self.ffmpeg_download(fmtname)

        stream_gears_download(self.raw_stream_url, self.fake_headers, filename, config.get('segment_time'),
                              config.get('file_size'))
        return True

    def streamlink_download(self, filename):  # streamlink+ffmpeg混合下载模式，适用于下载hls流
        streamlink_input_args = ['--stream-segment-threads', '3', '--hls-playlist-reload-attempts', '1']
        streamlink_cmd = ['streamlink', *streamlink_input_args, self.raw_stream_url, 'best', '-O']
        ffmpeg_input_args = ['-rw_timeout', '20000000']
        ffmpeg_cmd = ['ffmpeg', '-re', '-i', 'pipe:0', '-y', *ffmpeg_input_args, *self.default_output_args,
                      *self.opt_args, '-c', 'copy', '-f', self.suffix]
        # if config.get('segment_time'):
        #     ffmpeg_cmd += ['-f', 'segment',
        #              f'{filename} part-%03d.{self.suffix}']
        # else:
        #     ffmpeg_cmd += [
        #         f'{filename}.{self.suffix}.part']
        ffmpeg_cmd += [f'{filename}.{self.suffix}.part']
        streamlink_proc = subprocess.Popen(streamlink_cmd, stdout=subprocess.PIPE)
        ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=streamlink_proc.stdout, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT)
        try:
            with ffmpeg_proc.stdout as stdout:
                for line in iter(stdout.readline, b''):
                    decode_line = line.decode(errors='ignore')
                    print(decode_line, end='', file=sys.stderr)
                    logger.debug(decode_line.rstrip())
            retval = ffmpeg_proc.wait()
        except KeyboardInterrupt:
            if sys.platform != 'win32':
                ffmpeg_proc.communicate(b'q')
            raise
        if retval != 0:
            return False
        return True

    def ffmpeg_download(self, filename):
        default_input_args = ['-headers', ''.join('%s: %s\r\n' % x for x in self.fake_headers.items()), '-rw_timeout',
                              '20000000']
        parsed_url = urlparse(self.raw_stream_url)
        path = parsed_url.path
        if '.m3u8' in path:
            default_input_args += ['-max_reload', '1000']
        args = ['ffmpeg', '-y', *default_input_args,
                '-i', self.raw_stream_url, *self.default_output_args, *self.opt_args,
                '-c', 'copy', '-f', self.suffix]
        # if config.get('segment_time'):
        #     args += ['-f', 'segment',
        #              f'{filename} part-%03d.{self.suffix}']
        # else:
        #     args += [
        #         f'{filename}.{self.suffix}.part']
        args += [f'{filename}.{self.suffix}.part']

        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            with proc.stdout as stdout:
                for line in iter(stdout.readline, b''):  # b'\n'-separated lines
                    decode_line = line.decode(errors='ignore')
                    print(decode_line, end='', file=sys.stderr)
                    logger.debug(decode_line.rstrip())
            retval = proc.wait()
        except KeyboardInterrupt:
            if sys.platform != 'win32':
                proc.communicate(b'q')
            raise
        if retval != 0:
            return False
        return True

    async def danmaku_download_start(self, filename):
        pass

    def run(self):
        if not self.check_stream():
            return False
        file_name = self.file_name
        retval = self.download(file_name)
        logger.info(f'part: {file_name}.{self.suffix}')
        self.rename(f'{file_name}.{self.suffix}')
        return retval

    def start(self):
        logger.info(f'开始下载：{self.__class__.__name__} - {self.fname}')
        date = time.localtime()
        delay = int(config.get('delay', 0))
        # 重试次数
        retry_count = 0
        # delay 重试次数
        retry_count_delay = 0
        # delay 总重试次数 向上取整
        delay_all_retry_count = -(-delay // 60)

        while True:
            ret = False
            try:
                ret = self.run()
            except:
                logger.exception('Uncaught exception:')
            finally:
                self.close()
            if ret:
                if self.is_download:
                    # 成功下载后也不检测下一个需要下载的视频而是先上传等待下次检测保证上传时使用下载视频的标题
                    # 开启边录边传会快些
                    break
                # 成功下载重置重试次数
                retry_count = 0
                retry_count_delay = 0
            else:
                if retry_count < 3:
                    retry_count += 1
                    logger.info(f'获取流失败：{self.__class__.__name__} - {self.fname}，将在 10 秒后重试，重试次数 {retry_count} / 3')
                    time.sleep(10)
                    continue
                if self.is_download:
                    # 下载模式如果下载失败重试三次后直接跳出
                    break

                if delay:
                    retry_count_delay += 1
                    if retry_count_delay > delay_all_retry_count:
                        # logger.info(f'下播延迟检测结束：{self.__class__.__name__}:{self.fname}')
                        break
                    else:
                        if delay < 60:
                            logger.info(
                                f'下播延迟检测：{self.__class__.__name__} - {self.fname}，将在 {delay} 秒后检测开播状态')
                            time.sleep(delay)
                        else:
                            if retry_count_delay == 1:
                                # 只有第一次显示
                                logger.info(
                                    f'下播延迟检测：{self.__class__.__name__} - {self.fname}，每隔 60 秒检测开播状态，共检测 {delay_all_retry_count} 次')
                            time.sleep(60)
                        continue
                else:
                    break

        # 获取封面
        if self.use_live_cover and self.live_cover_url is not None:
            try:
                save_dir = f'cover/{self.__class__.__name__}/{self.fname}/'
                if not os.path.exists(save_dir):
                    os.makedirs(save_dir)
                fmtname = time.strftime(self.get_filename().encode("unicode-escape").decode(), date).encode().decode(
                    "unicode-escape")

                url_path = urlparse(self.live_cover_url).path
                suffix = None
                if '.jpg' in url_path:
                    suffix = 'jpg'
                elif '.png' in url_path:
                    suffix = 'png'

                if suffix:
                    live_cover_path = f'{save_dir}{fmtname}.{suffix}'
                    if os.path.exists(live_cover_path):
                        self.live_cover_path = live_cover_path
                    else:
                        response = requests.get(self.live_cover_url, headers=self.fake_headers, timeout=30)
                        with open(live_cover_path, 'wb') as f:
                            f.write(response.content)
                            f.close()
                            self.live_cover_path = live_cover_path
                    logger.info(
                        f'封面下载成功：{self.__class__.__name__} - {self.fname}：{os.path.abspath(self.live_cover_path)}')
                else:
                    logger.warning(f'封面下载失败：{self.__class__.__name__} - {self.fname}：封面格式不支持')

            except:
                logger.exception(f'封面下载失败：{self.__class__.__name__} - {self.fname}')

        logger.info(f'退出下载：{self.__class__.__name__} - {self.fname}')
        if config['streamers'].get(self.fname, {}).get('downloaded_processor'):
            downloaded_processor(config['streamers'].get(self.fname, {}).get('downloaded_processor'),
                                 f'{{"name": "{self.fname}", "url": "{self.url}", "room_title": "{self.room_title}", "start_time": "{time.strftime("%Y-%m-%d %H:%M:%S", date)}"}}')
        return {
            'name': self.fname,
            'url': self.url,
            'title': self.room_title,
            'date': date,
            'live_cover_path': self.live_cover_path,
            'is_download': self.is_download,
        }

    @staticmethod
    def rename(file_name):
        try:
            os.rename(file_name + '.part', file_name)
            logger.debug(f'更名 {file_name + ".part"} 为 {file_name}')
        except FileNotFoundError:
            logger.debug(f'FileNotFoundError: {file_name}')
        except FileExistsError:
            os.rename(file_name + '.part', file_name)
            logger.info(f'FileExistsError: 更名 {file_name + ".part"} 为 {file_name}')

    @property
    def file_name(self):
        if self.filename_prefix:  # 判断是否存在自定义录播命名设置
            filename = (self.filename_prefix.format(streamer=self.fname, title=self.room_title).encode(
                'unicode-escape').decode()).encode().decode("unicode-escape")
        else:
            filename = f'{self.fname}%Y-%m-%dT%H_%M_%S'
        filename = get_valid_filename(filename)
        return time.strftime(filename.encode("unicode-escape").decode()).encode().decode("unicode-escape")

    def close(self):
        pass


def stream_gears_download(url, headers, file_name, segment_time=None, file_size=None):
    class Segment:
        pass

    segment = Segment()
    if segment_time:
        seg_time = segment_time.split(':')
        print(int(seg_time[0]) * 60 * 60 + int(seg_time[1]) * 60 + int(seg_time[2]))
        segment.time = int(seg_time[0]) * 60 * 60 + int(seg_time[1]) * 60 + int(seg_time[2])
    if file_size:
        segment.size = file_size
    if file_size is None and segment_time is None:
        segment.size = 8 * 1024 * 1024 * 1024
    stream_gears.download(
        url,
        headers,
        file_name,
        segment
    )


def get_valid_filename(name):
    """
    Return the given string converted to a string that can be used for a clean
    filename. Remove leading and trailing spaces; convert other spaces to
    underscores; and remove anything that is not an alphanumeric, dash,
    underscore, or dot.
    # >>> get_valid_filename("john's portrait in 2004.jpg")
    >>> get_valid_filename("{self.fname}%Y-%m-%dT%H_%M_%S")
    '{self.fname}%Y-%m-%dT%H_%M_%S'
    """
    # s = str(name).strip().replace(" ", "_") #因为有些人会在主播名中间加入空格，为了避免和录播完毕自动改名冲突，所以注释掉
    s = re.sub(r"(?u)[^-\w.%{}\[\]【】「」\s]", "", str(name))
    if s in {"", ".", ".."}:
        raise RuntimeError("Could not derive file name from '%s'" % name)
    return s


def downloaded_processor(processors, data):
    for processor in processors:
        if processor.get('run'):
            try:
                process_output = subprocess.check_output(
                    processor['run'], shell=True,
                    input=data,
                    stderr=subprocess.STDOUT, text=True)
                logger.info(process_output.rstrip())
            except subprocess.CalledProcessError as e:
                logger.exception(e.output)
                continue

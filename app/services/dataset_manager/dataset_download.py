# Copyright (C) 2022-Present Indoc Systems
#
# Licensed under the GNU AFFERO GENERAL PUBLIC LICENSE,
# Version 3.0 (the "License") available at https://www.gnu.org/licenses/agpl-3.0.en.html.
# You may not use this file except in compliance with the License.

import datetime as dt
import os
import time
from typing import Any

import requests
from tqdm import tqdm

import app.services.logger_services.log_functions as logger
from app.configs.app_config import AppConfig
from app.configs.user_config import UserConfig
from app.models.service_meta_class import MetaService
from app.services.dataset_manager.model import EFileStatus
from app.services.output_manager.error_handler import ECustomizedError
from app.services.output_manager.error_handler import SrvErrorHandler
from app.services.output_manager.message_handler import SrvOutPutHandler

from ..user_authentication.decorator import require_valid_token


class SrvDatasetDownloadManager(metaclass=MetaService):
    def __init__(self, output_path, dataset_code, dataset_geid):
        self.user = UserConfig()
        self.output = output_path
        self.dataset_code = dataset_code
        self.dataset_geid = dataset_geid
        self.session_id = UserConfig().session_id
        self.hash_code = ''
        self.version = ''
        self.download_url = ''
        self.default_filename = ''

    @require_valid_token()
    def pre_dataset_version_download(self):
        url = AppConfig.Connections.url_dataset + f'/{self.dataset_geid}/download/pre'
        headers = {
            'Authorization': 'Bearer ' + self.user.access_token,
            'Refresh-token': self.user.refresh_token,
            'Session-ID': self.session_id,
        }
        payload = {'version': self.version}
        try:
            response = requests.get(url, headers=headers, params=payload)
            response.raise_for_status()
        except requests.HTTPError as e:
            response = e.response
            if response.status_code == 404:
                SrvErrorHandler.customized_handle(ECustomizedError.VERSION_NOT_EXIST, True, self.version)
            else:
                SrvErrorHandler.default_handle(response.content, True)
        return response.json()

    @require_valid_token()
    def pre_dataset_download(self) -> dict[str, Any]:
        url = AppConfig.Connections.url_dataset_v2download + '/download/pre'
        headers = {
            'Authorization': 'Bearer ' + self.user.access_token,
            'Refresh-token': self.user.refresh_token,
            'Session-ID': self.session_id,
        }
        payload = {'dataset_code': self.dataset_code, 'session_id': self.session_id, 'operator': self.user.username}
        try:
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
        except requests.HTTPError as e:
            response = e.response
            SrvErrorHandler.default_handle(response.content, True)

        return response.json()

    @require_valid_token()
    def download_status(self) -> EFileStatus:
        url = AppConfig.Connections.url_download_core + f'v1/download/status/{self.hash_code}'
        try:
            response = requests.get(url)
            response.raise_for_status()
        except requests.HTTPError as e:
            response = e.response
            SrvErrorHandler.default_handle(response.content, True)

        res_json = response.json()
        status = res_json.get('result').get('status')
        return EFileStatus(status)

    def check_download_preparing_status(self) -> EFileStatus:
        max_retries = 15
        retries = 1
        backoff = 1
        start = time.monotonic()
        while retries < max_retries:
            status = self.download_status()
            if status not in [EFileStatus.RUNNING, EFileStatus.WAITING]:
                return status

            logger.info(f'Waiting for download preparation to complete. Try again in {backoff} second(s)...')
            time.sleep(backoff)
            retries += 1
            backoff = min(backoff * 2, 5)

        logger.error('Download preparation timed out.')
        raise TimeoutError(f'Download preparation did not complete in {int(time.monotonic() - start)} seconds.')

    @require_valid_token()
    def send_download_request(self):
        filename = f'{self.dataset_code}'
        if self.version:
            filename += f'_{self.version}'
        filename += f'_{dt.datetime.now(tz=dt.timezone.utc).isoformat(sep="_")}.zip'
        output_path = self.avoid_duplicate_file_name(self.output.rstrip('/') + '/' + filename)

        logger.info('Start downloading...')
        with requests.get(self.download_url, stream=True, allow_redirects=True) as r:
            r.raise_for_status()

            self.total_size = int(r.headers.get('Content-length'))
            with open(output_path, 'wb') as file, tqdm(
                desc=f'Downloading {filename}',
                unit='iB',
                unit_scale=True,
                total=self.total_size,
                unit_divisor=1024,
                bar_format='{desc} |{bar:30} {percentage:3.0f}% {remaining}',
            ) as bar:
                for data in r.iter_content(chunk_size=1024):
                    size = file.write(data)
                    bar.update(size)
        return output_path

    def avoid_duplicate_file_name(self, filename):
        suffix = 1
        original_filename = filename
        file, ext = os.path.splitext(original_filename)
        while True:
            if os.path.isfile(filename):
                filename = file + f' ({suffix})' + ext
                suffix += 1
            else:
                if filename == original_filename:
                    break
                else:
                    logger.warning(f'{original_filename} already exist, file will be saved as {filename}')
                    break
        return filename

    @require_valid_token()
    def download_dataset(self):
        pre_result = self.pre_dataset_download()
        self.hash_code = pre_result.get('result').get('payload').get('hash_code')
        self.download_url = AppConfig.Connections.url_download_core + f'v1/download/{self.hash_code}'
        self.default_filename = pre_result.get('result').get('target_names')[0]
        self.default_filename = self.default_filename.split('/')[-1]
        status = self.check_download_preparing_status()
        SrvOutPutHandler.download_status(status)
        saved_filename = self.send_download_request()
        if os.path.isfile(saved_filename):
            SrvOutPutHandler.download_success(saved_filename)
        else:
            SrvErrorHandler.customized_handle(ECustomizedError.DOWNLOAD_FAIL, True)

    @require_valid_token()
    def download_dataset_version(self, version):
        self.version = version
        pre_result = self.pre_dataset_version_download()
        self.download_url = pre_result.get('result').get('source')
        saved_filename = self.send_download_request()
        if os.path.isfile(saved_filename):
            SrvOutPutHandler.download_success(saved_filename)
        else:
            SrvErrorHandler.customized_handle(ECustomizedError.DOWNLOAD_FAIL, True)

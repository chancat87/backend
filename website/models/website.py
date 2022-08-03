import os
import pathlib
import subprocess
import uuid

from django.db import models
from django.db.models import IntegerChoices
from django.utils.translation import gettext_lazy as _
from loguru import logger
from rest_framework import serializers

from base.base_model import BaseModel
from base.utils.format import format_completed_process
from base.utils.logger import plog
from common.models.User import User
from website.applications.app_factory import AppFactory
from website.applications.core.application import Application
from website.applications.core.dataclass import NewWebSiteConfig, SSLConfig, WebServerTypeEnum
from website.models.utils import enable_section, disable_section, get_section, insert_section
from website.utils.certificate import issuing_certificate

nginx_config_example = """
server {
    ########BASIC########

    listen 80;
    listen [::]:80;
    root {dir_path};
    server_name {domain};

    ########BASIC########

    ########SSL########
    
    #**#listen 443 ssl http2;
    #**#listen [::]:443 ssl http2;
    #**#ssl_certificate      /etc/letsencrypt/live/{domain}/fullchain.pem;
    #**#ssl_certificate_key     /etc/letsencrypt/live/{domain}/privkey.pem;
    #**#ssl_trusted_certificate /etc/letsencrypt/live/{domain}/chain.pem;

    #**#ssl_protocols TLSv1.1 TLSv1.2 TLSv1.3;
    #**#ssl_ciphers EECDH+CHACHA20:EECDH+CHACHA20-draft:EECDH+AES128:RSA+AES128:EECDH+AES256:RSA+AES256:EECDH+3DES:RSA+3DES:!MD5;
    #**#ssl_prefer_server_ciphers on;
    #**#ssl_session_cache shared:SSL:10m;
    #**#ssl_session_timeout 10m;
    #**#error_page 497  https://$host$request_uri;

    ########SSL########

    ########USER########

    # Please add your desired configuration here.

    ########USER########

    ########APP########

    index index.html;
    location / {
        try_files $uri $uri/ =404;
    }

    ########APP########


}"""


class WebsiteManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().select_related('user')


class WebsiteMethod:
    @staticmethod
    def get_ssl_config_default() -> dict:
        return {
            "certbot": {
                "email": "",
                "provider": None,
            },
            "path": {
                "certificate": "",
                "key": "",
            },
            "method": "http-01"
        }


class Website(BaseModel):
    """
    定义网站属性
    """

    class StatusType(IntegerChoices):
        READY = 0, "准备中"
        VALID = 1, "有效"
        SUSPEND = 2, "暂停"
        EXPIRED = 3, "过期"
        DISABLED = 4, "禁用"
        ERROR = 5, "错误"

    class WebServerType(IntegerChoices):
        # WebServerTypeEnum
        # from website.applications.core.dataclass import WebServerTypeEnum
        Nginx = 1
        Apache = 2
        Lighttpd = 3
        IIS = 4
        Tomcat = 5
        Caddy = 6

    user = models.ForeignKey(User, related_name="user", blank=True, on_delete=models.CASCADE, verbose_name="用户")
    name = models.CharField(verbose_name="名称", max_length=32)
    domain = models.CharField(max_length=1024, verbose_name="域名")
    extra_domain = models.CharField(max_length=1024, null=True, blank=True, verbose_name="additional domain name",
                                    help_text='use "," split domain name')
    # 配置说明
    # 证书签发主要依赖于 certbot ，需要定义
    # --preferred-challenges "验证方式"，其中验证方式为
    #   dns-01(自动处理DNS验证，请参考 User Guide https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins,
    #   tls-sni-01 (443端口验证), http (80端口验证)
    # --email "邮件地址"
    # -----------------------------------------------------------------------------------------------------------------|
    # | --server "SERVER"                 The three parameters here mainly define the authentication information       |
    # |                                      of different issuing agencies.                                            |
    # | --eab-kid "EAB_KID"               To be abstracted as an optional field letsencrypt, ZeroSSL,                  |
    # |                                      or a custom issuing authority                                             |
    # | --eab-hmac-key "EAB_HMAC_KEY"     These three items should be in the system configuration, and the user should |
    # |                                      not be differentiated globally.                                           |
    # -----------------------------------------------------------------------------------------------------------------|
    # certbot certonly -n --nginx --agree-tos -m email@gmail.com -d "domain.com"
    # specific reference：https://www.moec.top/archives/2544.
    #
    # ssl_config configuration instructions
    # certbot
    #  - email: Email, required
    #  - server: optional
    # path: The certificate path, the subsequent nginx configuration files will be based on this path
    #  - certificate: fullchain.pem file absolute path
    #  - key: privkey.pem file absolute path
    ssl_config = models.JSONField(default=WebsiteMethod.get_ssl_config_default, blank=True, null=True,
                                  verbose_name="证书配置")
    ssl_enable = models.BooleanField(default=False, verbose_name='enable SSL')
    index_root = models.CharField(default="/var/www/html", max_length=4096, verbose_name="站点目录")
    status = models.IntegerField(choices=StatusType.choices, default=StatusType.READY, verbose_name="状态",
                                 help_text=StatusType.labels)
    status_info = models.CharField(default='', blank=True, null=True, max_length=2048, verbose_name="status info")

    web_server_type = models.IntegerField(default=WebServerType.Nginx,
                                          choices=WebServerType.choices)
    application = models.CharField(max_length=64, verbose_name="application")
    application_config = models.JSONField(default=dict, verbose_name="application config")

    valid_web_server_config = models.TextField(max_length=102400, default=None, blank=True, null=True,
                                               verbose_name="valid configuration")

    class Meta:
        unique_together = ['name']
        verbose_name = "website"
        verbose_name_plural = "websites"

    def __str__(self):
        return self.name

    def __or_create_ssl_config(self):
        if self.ssl_config is None or self.ssl_config['certbot']['email'] == '':
            self.log('debug', f'{self.name} - {self.domain} ssl is first enable.')
            ssl_config = {
                "certbot": {
                    "email": self.user.email,
                    "provider": "default",
                },
                "path": {
                    "certificate": f"/etc/letsencrypt/live/{self.domain}/fullchain.pem",
                    "key": f"/etc/letsencrypt/live/{self.domain}/privkey.pem",
                },
                "method": "http-01"
            }

            ssl_folder = pathlib.Path(f'/etc/letsencrypt/live/{self.domain}')
            if not ssl_folder.exists():
                ssl_folder.mkdir()

            self.ssl_config = ssl_config

    def get_website_config(self) -> NewWebSiteConfig:
        config = NewWebSiteConfig(domain=self.domain, root_dir=self.index_root,
                                  web_server_type=WebServerTypeEnum.Nginx)
        if self.ssl_enable:
            plog.debug(f'enable {self.name} - {self.domain} ssl toggle.')

            self.__or_create_ssl_config()
            config.ssl_config = SSLConfig(ssl_certificate_path=self.ssl_config["path"]["certificate"],
                                          ssl_key_path=self.ssl_config["path"]["key"])
            plog.debug(config.ssl_config.__str__())
        if self.valid_web_server_config is not None:
            config.web_server_config = self.valid_web_server_config

        return config

    def get_application_module(self, config: NewWebSiteConfig) -> Application:
        app_factory = AppFactory
        app_factory.load()
        return app_factory.get_application_module(self.application, config)

    def is_valid_configuration(self):

        plog.info(f"verify {self.domain} configuration...\n\n{self.valid_web_server_config}")

        minimum_config = f"""user www-data;
              worker_processes auto;
              pid /run/nginx.pid;
              include /etc/nginx/modules-enabled/*.conf;
              events {{
                      worker_connections 768;
              }}

              http {{
                      sendfile on;
                      tcp_nopush on;
                      types_hash_max_size 2048;
                      include /etc/nginx/mime.types;
                      default_type application/octet-stream;
                      ssl_protocols TLSv1 TLSv1.1 TLSv1.2 TLSv1.3; # Dropping SSLv3, ref: POODLE
                      ssl_prefer_server_ciphers on;
                      gzip on;
                      {self.valid_web_server_config}

              }}
              """

        tmp_config = pathlib.Path(f'/tmp/{uuid.uuid4().hex}.conf')
        tmp_config.write_text(minimum_config)
        cmd = f'nginx -t -c {tmp_config.absolute()}'
        r = subprocess.run(cmd, shell=True, capture_output=True)

        nginx_config_path = f'/etc/nginx/sites-enabled/{self.domain}.conf'

        with open(nginx_config_path, 'w') as f:
            f.write(self.valid_web_server_config)

        return r

    def check_nginx_config(self):

        errors = {}
        r = self.is_valid_configuration()
        if r.returncode != 0:
            msg = ''
            if r.stdout:
                msg += r.stdout.decode('utf-8')
            if r.stderr:
                msg += '\n' + r.stderr.decode('utf-8')
            errors['valid_web_server_config'] = _('Invalid configuration:') + msg

        if errors:
            plog.error(f'{errors}')
            raise serializers.ValidationError(errors)

    def get_nginx_config(self):
        data = nginx_config_example.replace('{domain}', self.domain).replace('{dir_path}', self.index_root)
        if self.ssl_enable:
            data = enable_section(data, 'ssl')
        else:
            data = disable_section(data, 'ssl')
        return data

    def clean(self):
        self.check_nginx_config()
        os.system('systemctl reload nginx')


def website_pre_save(instance: Website):
    if instance.status == instance.StatusType.READY:
        plog.info('skip listener_website_save, because website status is ready.')
        return

    if instance.ssl_enable:
        p = issuing_certificate(instance)
        if p.returncode != 0:
            errors = {'ssl_enable': _('issue certificate error:\n') + format_completed_process(p)}

            logger.error(errors)
            raise serializers.ValidationError(errors)

    plog.debug(f"ssl_enable:{instance.ssl_enable}")

    app = instance.get_application_module(instance.get_website_config())
    app.update()
    app.reload()

    data = instance.get_nginx_config()
    user_config = get_section(instance.valid_web_server_config, 'user')

    data = insert_section(data, user_config, 'user')
    data = insert_section(data, app.read(), 'app')

    instance.valid_web_server_config = data
    instance.clean()
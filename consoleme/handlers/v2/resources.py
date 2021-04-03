import sys
from datetime import datetime, timedelta

import sentry_sdk
import ujson as json
from policy_sentry.util.arns import parse_arn

from consoleme.config import config
from consoleme.exceptions.exceptions import MustBeFte
from consoleme.handlers.base import BaseAPIV2Handler, BaseMtlsHandler
from consoleme.lib.account_indexers import get_account_id_to_name_mapping
from consoleme.lib.auth import can_admin_policies
from consoleme.lib.aws import fetch_resource_details
from consoleme.lib.plugins import get_plugin_by_name
from consoleme.lib.policies import get_url_for_resource

log = config.get_logger()
stats = get_plugin_by_name(config.get("plugins.metrics", "default_metrics"))()
aws = get_plugin_by_name(config.get("plugins.aws", "default_aws"))()
group_mapping = get_plugin_by_name(
    config.get("plugins.group_mapping", "default_group_mapping")
)()
auth = get_plugin_by_name(config.get("plugins.auth", "default_auth"))()
internal_policies = get_plugin_by_name(
    config.get("plugins.internal_policies", "default_policies")
)()


class ResourceDetailHandler(BaseAPIV2Handler):
    async def get(self, account_id, resource_type, region=None, resource_name=None):
        if not self.user:
            return
        if config.get("policy_editor.disallow_contractors", True) and self.contractor:
            if self.user not in config.get(
                "groups.can_bypass_contractor_restrictions", []
            ):
                raise MustBeFte("Only FTEs are authorized to view this page.")
        read_only = False

        can_save_delete = (can_admin_policies(self.user, self.groups),)

        account_id_for_arn: str = account_id
        if resource_type == "s3":
            account_id_for_arn = ""
        arn = f"arn:aws:{resource_type}:{region or ''}:{account_id_for_arn}:{resource_name}"

        stats.count(
            "ResourcePolicyEditHandler.get", tags={"user": self.user, "arn": arn}
        )

        log_data = {
            "user": self.user,
            "ip": self.ip,
            "function": f"{__name__}.{self.__class__.__name__}.{sys._getframe().f_code.co_name}",
            "message": "Incoming request",
            "user-agent": self.request.headers.get("User-Agent"),
            "request_id": self.request_uuid,
            "arn": arn,
        }

        log.debug(log_data)

        error = ""

        try:
            resource_details = await fetch_resource_details(
                account_id, resource_type, resource_name, region
            )
        except Exception as e:
            sentry_sdk.capture_exception()
            log.error({**log_data, "error": e}, exc_info=True)
            resource_details = None
            error = str(e)

        if not resource_details:
            self.send_error(
                404,
                message=(
                    f"Unable to retrieve the specified {resource_type} resource: "
                    f"{account_id}/{resource_name}/{region}. {error}",
                ),
            )
            return

        # TODO: Get S3 errors for s3 buckets only, else CT errors
        yesterday = (datetime.today() - timedelta(days=1)).strftime("%Y%m%d")
        s3_query_url = None
        if resource_type == "s3":
            s3_query_url = config.get("s3.bucket_query_url")
        all_s3_errors = None
        if s3_query_url:
            s3_query_url = s3_query_url.format(
                yesterday=yesterday, bucket_name=f"'{resource_name}'"
            )
            s3_error_topic = config.get("redis.s3_errors", "S3_ERRORS")
            all_s3_errors = self.red.get(s3_error_topic)

        s3_errors = []
        if all_s3_errors:
            s3_errors = json.loads(all_s3_errors).get(arn, [])

        account_ids_to_name = await get_account_id_to_name_mapping()
        # TODO(ccastrapel/psanders): Make a Swagger spec for this
        self.write(
            dict(
                arn=arn,
                resource_details=resource_details,
                account_id=account_id,
                account_name=account_ids_to_name.get(account_id, None),
                read_only=read_only,
                can_save_delete=can_save_delete,
                s3_errors=s3_errors,
                error_url=s3_query_url,
                config_timeline_url=resource_details.get("config_timeline_url"),
            )
        )


class GetResourceURLHandler(BaseMtlsHandler):
    """consoleme CLI resource URL handler. Parameters accepted: arn."""

    def check_xsrf_cookie(self):
        pass

    def initialize(self):
        self.user: str = None
        self.eligible_roles: list = []

    async def get(self):
        """
        /api/v2/get_resource_url - Endpoint used to get an URL from an ARN
        ---
        get:
            description: Get the resource URL for ConsoleMe, given an ARN
            responses:
                200:
                    description: Returns a URL generated from the ARN in JSON form
                400:
                    description: Malformed Request
                403:
                    description: Forbidden
        """
        self.user: str = self.requester["email"]
        arn: str = self.get_argument("arn", None)
        if not arn:
            self.write_error(400, message="arn is a required parameter")
            return

        log_data = {
            "function": f"{__name__}.{self.__class__.__name__}.{sys._getframe().f_code.co_name}",
            "user": self.user,
            "arn": arn,
            "message": "Generating URL for resource",
            "user-agent": self.request.headers.get("User-Agent"),
            "request_id": self.request_uuid,
        }
        log.debug(log_data)
        stats.count("GetResourceURL.get", tags={"user": self.user})
        try:
            # parse_arn will raise an exception on invalid arns
            parse_arn(arn)
            resource_url = await get_url_for_resource(arn)
        except Exception as e:
            self.write_error(400, message=str(e))
            return

        self.write({"url": resource_url})
        await self.finish()

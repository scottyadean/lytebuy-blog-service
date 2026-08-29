""" Shared AWS connection helper.

One place to build boto3 clients/resources so credential resolution is identical
everywhere. Deployed, the Lambda's execution role supplies credentials through
the default chain. Locally you set AWS_PROFILE (or the AWS_SESSION_TOKEN trio)
before starting `sls offline`, and boto_connect picks it up.
"""
import os

import boto3


def boto_connect(service, region=None, resource=False):
    """
        Create a boto3 client.
        Assumes default AWS connection environment variables.
        NOTE: you init sls offline with env vars at the command line.
        NOTE: if you are working on your local be sure to set: AWS_PROFILE
        :param service <string> name of boto service
        :param region <string> aws region
        :param resource <bool> return a resource instead of a client
        :return <object>
    """

    if region is None:
        region = os.environ.get('REGION', None)

    if not region and 'AWS_DEFAULT_REGION' in os.environ:
        region = os.environ['AWS_DEFAULT_REGION']

    funct = boto3.client if resource is False else boto3.resource

    if 'AWS_SESSION_TOKEN' in os.environ:
        return funct(service, region,
                            aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
                            aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
                            aws_session_token=os.environ['AWS_SESSION_TOKEN'])
    if 'AWS_PROFILE' in os.environ:
        session = boto3.Session(profile_name=os.environ['AWS_PROFILE'], region_name=region)
        return session.client(service, region) if resource is False else session.resource(service, region)

    return funct(service, region)

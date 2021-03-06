# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013, 2014 Scalr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from gevent import monkey
monkey.patch_all()

import os
import sys

cwd = os.path.dirname(os.path.abspath(__file__))
scalrpy_dir = os.path.join(cwd, '..')
sys.path.insert(0, scalrpy_dir)

import time
import uuid
import json
import socket
import gevent
import urllib2
import urlparse
import binascii

import boto.ec2
import boto.exception
import boto.ec2.regioninfo

import libcloud.common.types
from libcloud.compute.types import Provider, NodeState
from libcloud.compute.providers import get_driver
import libcloud.security
libcloud.security.VERIFY_SSL_CERT = False

import httplib2
import googleapiclient
from googleapiclient.discovery import build
from oauth2client.client import SignedJwtAssertionCredentials

from scalrpy.util import helper
from scalrpy.util import dbmanager
from scalrpy.util import analytics
from scalrpy.util import cryptotool
from scalrpy.util import exceptions
from scalrpy.util import application
from scalrpy.util.analytics import platforms

from scalrpy import LOG


helper.patch_gevent()


url_map = {
    'cloudstack': 'api_url',
    'ec2': None,
    'ecs': 'keystone_url',
    'eucalyptus': 'ec2_url',
    'gce': None,
    'idcf': 'api_url',
    'openstack': 'keystone_url',
    'rackspacenguk': 'keystone_url',
    'rackspacengus': 'keystone_url',
    'ocs': 'keystone_url',
    'nebula': 'keystone_url',
}


os_map = {
    'linux': 0,
    'windows': 1,
    None: 0
}


app = None



@helper.retry(1, 5, urllib2.URLError, socket.timeout)
def _libcloud_list_locations(driver):
    return driver.list_locations()



@helper.retry(1, 5, urllib2.URLError, socket.timeout)
def _libcloud_list_nodes(driver):
    return driver.list_nodes()



@helper.retry(1, 5, urllib2.URLError, socket.timeout)
def _ec2_get_only_instances(ec2conn):
    return ec2conn.get_only_instances(filters={'instance-state-name': 'running'})



@helper.retry(1, 5, urllib2.URLError, socket.timeout)
def _libcloud_get_service_catalog(driver):
    return driver.connection.get_service_catalog().get_catalog()



def _handle_exception(e, msg):
    if type(e) == boto.exception.EC2ResponseError and e.status in [401, 403]:
        LOG.warning(msg)
    elif type(e) in [
            libcloud.common.types.InvalidCredsError,
            libcloud.common.types.LibcloudError,
            libcloud.common.types.MalformedResponseError,
            gevent.timeout.Timeout,
            socket.timeout,
            socket.gaierror]:
        LOG.warning(msg)
    elif type(e) == socket.error and e.errno in [111, 113]:
        LOG.warning(msg)
    elif type(e) == googleapiclient.errors.HttpError and e.resp['status'] in ['403']:
        LOG.warning(msg)
    else:
        LOG.error(msg)



def _ec2_region(region, cred):
    access_key = cryptotool.decrypt_scalr(app.crypto_key, cred['access_key'])
    secret_key = cryptotool.decrypt_scalr(app.crypto_key, cred['secret_key'])
    kwds = {
        'aws_access_key_id': access_key,
        'aws_secret_access_key': secret_key
    }
    if app.scalr_config.get('aws', {}).get('use_proxy', False) in [True, 'yes']:
        if app.scalr_config['connections'].get('proxy', {}).get('use_on', 'both') in ['both', 'scalr']:
            kwds['proxy'] = app.scalr_config['connections']['proxy']['host']
            kwds['proxy_port'] = app.scalr_config['connections']['proxy']['port']
            kwds['proxy_user'] = app.scalr_config['connections']['proxy']['user']
            kwds['proxy_pass'] = app.scalr_config['connections']['proxy']['pass']
    conn = boto.ec2.connect_to_region(region, **kwds)
    cloud_nodes = _ec2_get_only_instances(conn)
    timestamp = int(time.time())
    nodes = list()
    for cloud_node in cloud_nodes:
        node = {
            'instance_id': cloud_node.id,
            'instance_type': cloud_node.instance_type,
            'os': cloud_node.platform if cloud_node.platform else 'linux'
        }
        nodes.append(node)
    return {
        'region': region,
        'timestamp': timestamp,
        'nodes': nodes
    } if nodes else dict()



def ec2(cred):
    """
    :returns: list
        [{'region': str, 'timestamp': int, 'nodes': list}]
    """

    result = list()

    if cred['account_type'] == 'regular':
        regions = [
            'us-east-1',
            'us-west-2',
            'us-west-1',
            'eu-west-1',
            'eu-central-1',
            'ap-southeast-1',
            'ap-southeast-2',
            'ap-northeast-1',
            'sa-east-1',
        ]
    elif cred['account_type'] == 'gov-cloud':
        regions = [
            'us-gov-west-1',
        ]
    elif cred['account_type'] == 'cn-cloud':
        regions = [
            'cn-north-1',
        ]
    else:
        msg = 'Unsupported account type for ec2 platform: {0}'.format(cred['account_type'])
        raise Exception(msg)

    app.pool.wait()
    async_results = dict(
        (region, app.pool.apply_async(_ec2_region, args=(region, cred,)))
        for region in regions
    )
    gevent.sleep(0)  # force switch
    timeout = app.config['cloud_connection_timeout'] + 1
    for region, async_result in async_results.iteritems():
        try:
            region_nodes = async_result.get(timeout=timeout)
            if region_nodes:
                result.append(region_nodes)
        except:
            async_result.kill()
            e = sys.exc_info()[1]
            msg = 'platform: {platform}, region: {region}, env_id: {env_id}, reason: {error}'
            msg = msg.format(
                    platform=cred.platform, region=region,
                    env_id=cred.env_id, error=helper.exc_info())
            _handle_exception(e, msg)
    return result



def _eucalyptus(cred):
    access_key = cryptotool.decrypt_scalr(app.crypto_key, cred['access_key'])
    secret_key = cryptotool.decrypt_scalr(app.crypto_key, cred['secret_key'])
    ec2_url = cryptotool.decrypt_scalr(app.crypto_key, cred['ec2_url'])
    url = urlparse.urlparse(ec2_url)
    splitted_netloc = url.netloc.split(':')
    host = splitted_netloc[0]
    try:
        port = splitted_netloc[1]
    except:
        port = None
    path = url.path
    region = 'eucalyptus'
    region_info = boto.ec2.regioninfo.RegionInfo(name=region, endpoint=host)
    conn = boto.connect_ec2(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        is_secure=False,
        port=port,
        path=path,
        region=region_info
    )
    cloud_nodes = _ec2_get_only_instances(conn)
    timestamp = int(time.time())
    nodes = list()
    for cloud_node in cloud_nodes:
        node = {
            'instance_id': cloud_node.id,
            'instance_type': cloud_node.instance_type,
            'os': cloud_node.platform if cloud_node.platform else 'linux'
        }
        nodes.append(node)
    return {
        'region': cred['group'],
        'timestamp': timestamp,
        'nodes': nodes
    } if nodes else dict()



def eucalyptus(cred):
    """
    :returns: list
        [{'region': str, 'timestamp': int, 'nodes': list}]
    """

    result = list()

    app.pool.wait()
    async_result = app.pool.apply_async(_eucalyptus, args=(cred,))
    gevent.sleep(0)  # force switch
    try:
        cloud_nodes = async_result.get(timeout=app.config['cloud_connection_timeout'] + 1)
        if cloud_nodes:
            result.append(cloud_nodes)
    except:
        async_result.kill()
        e = sys.exc_info()[1]
        msg = 'platform: {platform}, env_id: {env_id}, reason: {error}'
        msg = msg.format(platform=cred.platform, env_id=cred.env_id, error=helper.exc_info())
        _handle_exception(e, msg)
    return result



def _cloudstack(cred):
    result = list()
    api_key = cryptotool.decrypt_scalr(app.crypto_key, cred['api_key'])
    secret_key = cryptotool.decrypt_scalr(app.crypto_key, cred['secret_key'])
    api_url = cryptotool.decrypt_scalr(app.crypto_key, cred['api_url'])
    url = urlparse.urlparse(api_url)
    splitted_netloc = url.netloc.split(':')
    host = splitted_netloc[0]
    try:
        port = splitted_netloc[1]
    except:
        port = 443 if url.scheme == 'https' else None
    path = url.path
    secure = url.scheme == 'https'

    cls = get_driver(Provider.CLOUDSTACK)
    driver = cls(key=api_key, secret=secret_key, host=host, port=port, path=path, secure=secure)
    locations = driver.list_locations()
    cloud_nodes = _libcloud_list_nodes(driver)
    timestamp = int(time.time())
    for location in locations:
        nodes = list()
        for cloud_node in cloud_nodes:
            if cloud_node.state != NodeState.RUNNING or cloud_node.extra['zone_id'] != location.id:
                continue
            node = {
                'instance_id': cloud_node.id,
                'instance_type': cloud_node.extra['size_id'],
                'os': None
            }
            nodes.append(node)
        if nodes:
            result.append(
                {
                    'region': location.name,
                    'timestamp': timestamp,
                    'nodes': nodes
                }
            )
    return result



def cloudstack(cred):
    """
    :returns: list
        [{'region': str, 'timestamp': int, 'nodes': list}]
    """

    result = list()

    app.pool.wait()
    async_result = app.pool.apply_async(_cloudstack, args=(cred,))
    gevent.sleep(0)  # force switch
    try:
        result = async_result.get(timeout=app.config['cloud_connection_timeout'] + 1)
    except:
        async_result.kill()
        e = sys.exc_info()[1]
        msg = 'platform: {platform}, env_id: {env_id}, reason: {error}'
        msg = msg.format(platform=cred.platform, env_id=cred.env_id, error=helper.exc_info())
        _handle_exception(e, msg)
    return result



def idcf(cred):
    """
    :returns: list
        [{'region': str, 'timestamp': int, 'nodes': list}]
    """

    return cloudstack(cred)


def _gce_conn(cred):
    service_account_name = cryptotool.decrypt_scalr(app.crypto_key, cred['service_account_name'])
    if 'json_key' in cred:
        key = json.loads(cryptotool.decrypt_scalr(app.crypto_key, cred['json_key']))['private_key']
    else:
        key = cryptotool.decrypt_scalr(app.crypto_key, cred['key'])
        # convert pkcs12 to rsa
        out, err, ret_code = helper.call(
            "openssl pkcs12 -nodes -nocerts -passin pass:notasecret | openssl rsa",
            input=binascii.a2b_base64(key),
            shell=True
        )
        key = out.strip()

    signed_jwt_assert_cred = SignedJwtAssertionCredentials(
        service_account_name,
        key,
        ['https://www.googleapis.com/auth/compute']
    )
    http = httplib2.Http()
    http = signed_jwt_assert_cred.authorize(http)
    return build('compute', 'v1', http=http), http



def _gce_zone(zone, cred):
    conn, http = _gce_conn(cred)
    project_id = cryptotool.decrypt_scalr(app.crypto_key, cred['project_id'])
    request = conn.instances().list(
        project=project_id,
        zone=zone,
        filter='status eq RUNNING'
    )
    resp = request.execute(http=http)
    timestamp = int(time.time())
    cloud_nodes = resp['items'] if 'items' in resp else []
    nodes = list()
    for cloud_node in cloud_nodes:
        node = {
            'instance_id': cloud_node['id'],
            'server_name': cloud_node['name'],
            'instance_type': cloud_node['machineType'].split('/')[-1],
            'os': None
        }
        nodes.append(node)
    return {
        'region': zone,
        'timestamp': timestamp,
        'nodes': nodes
    } if nodes else dict()



def gce(cred):
    """
    :returns: list
        [{'region': str, 'timestamp': int, 'nodes': list}]
    """

    result = list()

    project_id = cryptotool.decrypt_scalr(app.crypto_key, cred['project_id'])
    try:
        conn, http = _gce_conn(cred)
        request = conn.zones().list(project=project_id)
        resp = request.execute(http=http)
    except:
        e = sys.exc_info()[1]
        msg = 'platform: {platform}, env_id: {env_id}, reason: {error}'
        msg = msg.format(platform=cred.platform, env_id=cred.env_id, error=helper.exc_info())
        _handle_exception(e, msg)
        return result

    zones = [_['name'] for _ in resp['items']] if 'items' in resp else []

    app.pool.wait()
    async_results = dict(
        (zone, app.pool.apply_async(_gce_zone, args=(zone, cred,)))
        for zone in zones
    )
    gevent.sleep(0)  # force switch
    for zone, async_result in async_results.iteritems():
        try:
            zone_nodes = async_result.get(timeout=app.config['cloud_connection_timeout'] + 1)
            if zone_nodes:
                result.append(zone_nodes)
        except:
            async_result.kill()
            e = sys.exc_info()[1]
            msg = 'platform: GCE, zone: {zone}, env_id: {env_id}, reason: {error}'
            msg = msg.format(zone=zone, env_id=cred.env_id, error=helper.exc_info())
            _handle_exception(e, msg)
    return result



def _openstack_cred(cred):
    username = cryptotool.decrypt_scalr(app.crypto_key, cred['username'])
    if 'password' in cred:
        password = cryptotool.decrypt_scalr(app.crypto_key, cred['password'])
        auth_version = '2.0_password'
    else:
        password = cryptotool.decrypt_scalr(app.crypto_key, cred['api_key'])
        auth_version = '2.0_apikey'
    keystone_url = cryptotool.decrypt_scalr(app.crypto_key, cred['keystone_url'])
    if not keystone_url.rstrip('/').endswith('/tokens'):
        keystone_url = os.path.join(keystone_url, 'tokens')
    if 'tenant_name' in cred:
        tenant_name = cryptotool.decrypt_scalr(app.crypto_key, cred['tenant_name'])
    else:
        tenant_name = None
    return username, password, auth_version, keystone_url, tenant_name



def _openstack_region(provider, service_name, region, cred):
    username, password, auth_version, keystone_url, tenant_name = _openstack_cred(cred)
    url = urlparse.urlparse(keystone_url)
    service_type = 'compute'

    cls = get_driver(provider)
    driver = cls(
        username,
        password,
        ex_force_auth_url=url.geturl(),
        ex_tenant_name=tenant_name,
        ex_force_auth_version=auth_version,
        ex_force_service_region=region,
        ex_force_service_type=service_type,
        ex_force_service_name=service_name,
    )
    cloud_nodes = _libcloud_list_nodes(driver)
    timestamp = int(time.time())
    nodes = list()
    for cloud_node in cloud_nodes:
        if cloud_node.state != NodeState.RUNNING:
            continue
        node = {
            'instance_id': cloud_node.id,
            'instance_type': cloud_node.extra['flavorId'],
            'os': None
        }
        nodes.append(node)
    return {
        'region': region,
        'timestamp': timestamp,
        'nodes': nodes
    } if nodes else dict()



def _openstack(provider, cred):
    result = list()

    username, password, auth_version, keystone_url, tenant_name = _openstack_cred(cred)
    url = urlparse.urlparse(keystone_url)

    cls = get_driver(provider)
    driver = cls(
        username,
        password,
        ex_force_auth_url=url.geturl(),
        ex_force_base_url='%s://%s' % (url.scheme, url.netloc),
        ex_tenant_name=tenant_name,
        ex_force_auth_version=auth_version,
    )

    try:
        service_catalog = _libcloud_get_service_catalog(driver)
    except:
        e = sys.exc_info()[1]
        msg = (
                'platform: {platform}, env_id: {env_id}, url: {url}, tenant_name: {tenant_name}, '
                'auth_version: {auth_version}, reason: {error}')
        msg = msg.format(
                platform=cred.platform, env_id=cred.env_id, url=url, tenant_name=tenant_name,
                auth_version=auth_version, error=helper.exc_info())
        _handle_exception(e, msg)
        return result

    service_type = 'compute'
    service_names = service_catalog[service_type].keys()

    for service_name in service_names:
        regions = service_catalog[service_type][service_name].keys()
        app.pool.wait()
        async_results = dict(
            (
                region,
                app.pool.apply_async(
                    _openstack_region,
                    args=(provider, service_name, region, cred)
                )
            ) for region in regions
        )
        gevent.sleep(0)  # force switch
        for region, async_result in async_results.iteritems():
            try:
                region_nodes = async_result.get(timeout=app.config['cloud_connection_timeout'] + 1)
                if region_nodes:
                    result.append(region_nodes)
            except:
                async_result.kill()
                e = sys.exc_info()[1]
                msg = (
                        'platform: {platform}, env_id: {env_id}, url: {url}, '
                        'tenant_name: {tenant_name}, service_name={service_name}, '
                        'region: {region}, auth_version: {auth_version}, reason: {error}')
                msg = msg.format(
                        platform=cred.platform, env_id=cred.env_id, url=url, tenant_name=tenant_name,
                        service_name=service_name, region=region, auth_version=auth_version,
                        error=helper.exc_info())
                _handle_exception(e, msg)
    return result



def openstack(cred):
    """
    :returns: list
        [{'region': str, 'timestamp': int, 'nodes': list}]
    """

    return _openstack(Provider.OPENSTACK, cred)



def ecs(cred):
    """
    :returns: list
        [{'region': str, 'timestamp': int, 'nodes': list}]
    """

    return _openstack(Provider.OPENSTACK, cred)



def rackspacenguk(cred):
    """
    :returns: list
        [{'region': str, 'timestamp': int, 'nodes': list}]
    """

    return _openstack(Provider.RACKSPACE, cred)



def rackspacengus(cred):
    """
    :returns: list
        [{'region': str, 'timestamp': int, 'nodes': list}]
    """

    return _openstack(Provider.RACKSPACE, cred)



def ocs(cred):
    """
    :returns: list
        [{'region': str, 'timestamp': int, 'nodes': list}]
    """

    return _openstack(Provider.OPENSTACK, cred)



def nebula(cred):
    """
    :returns: list
        [{'region': str, 'timestamp': int, 'nodes': list}]
    """

    return _openstack(Provider.OPENSTACK, cred)



def _url_key_and_cloud_location_key(cred):
    platform = cred.platform
    if platform in ['cloudstack', 'idcf']:
        cloud_location_key = 'cloudstack.cloud_location'
        url_key = 'api_url'
    elif platform in ['openstack', 'ecs', 'rackspacenguk', 'rackspacengus', 'ocs', 'nebula']:
        cloud_location_key = 'openstack.cloud_location'
        url_key = 'keystone_url'
    elif platform == 'eucalyptus':
        cloud_location_key = 'euca.region'
        url_key = 'ec2_url'
    elif platform == 'ec2':
        cloud_location_key = 'ec2.region'
        url_key = None
    else:
        url_key = None
    return url_key, cloud_location_key



def sort_nodes(cloud_data, cred, envs_ids):
    platform = cred.platform

    # gce
    if platform == 'gce':
        for region_data in cloud_data:
            region_data['managed'] = list()
            region_data['not_managed'] = list()
            for node in region_data['nodes']:
                query = (
                        "SELECT server_id, env_id "
                        "FROM servers "
                        "WHERE server_id='{server_id}'"
                ).format(server_id=node['server_name'])
                result = app.scalr_db.execute(query, retries=1)
                if not result:
                    query = (
                            "SELECT server_id, env_id "
                            "FROM servers_history "
                            "WHERE server_id='{server_id}'"
                    ).format(server_id=node['server_name'])
                    result = app.scalr_db.execute(query, retries=1)
                if result and result[0]['env_id'] in envs_ids:
                    node['env_id'] = result[0]['env_id']
                    node['server_id'] = node['server_name']
                    region_data['managed'].append(node)
                else:
                    region_data['not_managed'].append(node)
            del region_data['nodes']
        return cloud_data

    # all platforms exclude gce
    envs_ids = list(set(_ for _ in envs_ids if _ or _ == 0))
    if not envs_ids:
        return tuple()

    url_key, cloud_location_key = _url_key_and_cloud_location_key(cred)
    url = cred[url_key] if url_key else ''

    for region_data in cloud_data:
        cloud_location = region_data['region']
        instances_ids = list(set(
            str(node['instance_id'])
            for node in region_data['nodes'] if node['instance_id'] or node['instance_id'] == 0))
        if not instances_ids:
            continue
        results = tuple()
        i, chunk_size = 0, 200
        while True:
            chunk_ids = instances_ids[i * chunk_size:(i + 1) * chunk_size]
            if not chunk_ids:
                break
            if url:
                query1 = (
                        "SELECT sp.server_id, sp.value AS instance_id, s.env_id "
                        "FROM server_properties sp "
                        "JOIN servers s ON sp.server_id=s.server_id "
                        "JOIN client_environment_properties cep ON s.env_id=cep.env_id "
                        "WHERE sp.name='{name}' "
                        "AND s.platform='{platform}' "
                        "AND s.cloud_location='{cloud_location}' "
                        "AND cep.name='{platform}.{url_key}' "
                        "AND cep.value='{url}' "
                        "AND sp.value IN ({value})"
                ).format(
                        name=analytics.Analytics.server_id_map[platform],
                        platform=platform,
                        cloud_location=cloud_location,
                        url_key=url_key,
                        url=url,
                        value=str(chunk_ids)[1:-1])
                query2 = (
                        "SELECT sp1.server_id, sp1.value AS instance_id, s.env_id "
                        "FROM server_properties sp1 "
                        "JOIN server_properties sp2 ON sp1.server_id=sp2.server_id "
                        "JOIN servers_history s ON sp1.server_id=s.server_id "
                        "JOIN client_environment_properties cep ON s.env_id=cep.env_id "
                        "WHERE s.platform='{platform}' "
                        "AND sp1.name='{name1}' "
                        "AND sp1.value IN ({value1}) "
                        "AND sp2.name='{name2}' "
                        "AND sp2.value='{value2}' "
                        "AND cep.name='{platform}.{url_key}' "
                        "AND cep.value='{url}'"
                ).format(
                        name1=analytics.Analytics.server_id_map[platform],
                        value1=str(chunk_ids)[1:-1],
                        name2=cloud_location_key,
                        value2=cloud_location,
                        platform=platform,
                        url_key=url_key,
                        url=url)
            else:
                query1 = (
                        "SELECT sp.server_id, sp.value AS instance_id, s.env_id "
                        "FROM server_properties sp "
                        "JOIN servers s ON sp.server_id=s.server_id "
                        "WHERE sp.name='{name}' "
                        "AND s.platform='{platform}' "
                        "AND s.cloud_location='{cloud_location}' "
                        "AND sp.value IN ({value})"
                ).format(
                        name=analytics.Analytics.server_id_map[platform],
                        platform=platform,
                        cloud_location=cloud_location,
                        value=str(chunk_ids)[1:-1])
                query2 = (
                        "SELECT sp1.server_id, sp1.value AS instance_id, s.env_id "
                        "FROM server_properties sp1 "
                        "JOIN server_properties sp2 ON sp1.server_id=sp2.server_id "
                        "JOIN servers_history s ON sp1.server_id=s.server_id "
                        "WHERE s.platform='{platform}' "
                        "AND sp1.name='{name1}' "
                        "AND sp1.value IN ({value1}) "
                        "AND sp2.name='{name2}' "
                        "AND sp2.value='{value2}'"
                ).format(
                        name1=analytics.Analytics.server_id_map[platform],
                        value1=str(chunk_ids)[1:-1],
                        name2=cloud_location_key,
                        value2=cloud_location,
                        platform=platform)
            chunk_results = app.scalr_db.execute(query1, retries=1)
            chunk_results_ids = [_['instance_id'] for _ in chunk_results]
            missing_ids = [_ for _ in chunk_ids if _ not in chunk_results_ids]
            if missing_ids:
                chunk_results = chunk_results + app.scalr_db.execute(query2, retries=1)
            if not chunk_results:
                break
            results += chunk_results
            i += 1

        managed = dict(
            (result['instance_id'], {'env_id': result['env_id'], 'server_id': result['server_id']})
            for result in results)

        region_data['managed'] = list()
        region_data['not_managed'] = list()
        for node in region_data['nodes']:
            instance_id = node['instance_id']
            if instance_id in managed:
                if managed[instance_id]['env_id'] in envs_ids:
                    node.update({
                        'env_id': managed[instance_id]['env_id'],
                        'server_id': managed[instance_id]['server_id'],
                    })
                    region_data['managed'].append(node)
            else:
                region_data['not_managed'].append(node)
        del region_data['nodes']
    return cloud_data



def sorted_data_update(sorted_data):
    for region_data in sorted_data:
        for server in region_data['managed']:
            if server['os'] is not None:
                continue
            query = (
                    "SELECT os_type "
                    "FROM servers "
                    "WHERE server_id='{server_id}'"
            ).format(server_id=server['server_id'])
            result = app.scalr_db.execute(query, retries=1)
            if not result:
                query = (
                        "SELECT value AS os_type "
                        "FROM server_properties "
                        "WHERE server_id='{server_id}' "
                        "AND name='os_type'"
                ).format(server_id=server['server_id'])
                result = app.scalr_db.execute(query, retries=1)
            if not result:
                server['os'] = 'linux'
                msg = "Can't detect os_type for server: {0}, set 'linux'".format(server['server_id'])
                LOG.warning(msg)
            else:
                server['os'] = result[0]['os_type']



def db_update(sorted_data, envs_ids, cred):
    platform = cred.platform

    for env_id in envs_ids:
        for region_data in sorted_data:
            try:
                sid = uuid.uuid4()
                if platform == 'ec2' and 'account_id' in cred:
                    cloud_account = cryptotool.decrypt_scalr(app.crypto_key, cred['account_id'])
                else:
                    cloud_account = None

                if url_map[platform]:
                    url = urlparse.urlparse(cryptotool.decrypt_scalr(
                            app.crypto_key, cred[url_map[platform]]).rstrip('/'))
                    url = '%s%s' % (url.netloc, url.path)
                else:
                    url = ''

                query = (
                        "SELECT client_id "
                        "FROM client_environments "
                        "WHERE id={env_id}"
                ).format(env_id=env_id)
                results = app.scalr_db.execute(query, retries=1)
                account_id = results[0]['client_id']

                query = (
                        "INSERT IGNORE INTO poller_sessions "
                        "(sid, account_id, env_id, dtime, platform, url, cloud_location, cloud_account) "
                        "VALUES "
                        "(UNHEX('{sid}'), {account_id}, {env_id}, '{dtime}', '{platform}', '{url}',"
                        "'{cloud_location}', '{cloud_account}')"
                ).format(
                    sid=sid.hex, account_id=account_id, env_id=env_id,
                    dtime=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(region_data['timestamp'])),
                    platform=platform, url=url, cloud_location=region_data['region'],
                    cloud_account=cloud_account
                )
                app.analytics_db.execute(query, retries=1)

                # managed
                for managed in region_data['managed']:
                    if managed['env_id'] != env_id:
                        continue
                    query = (
                            "INSERT IGNORE INTO managed "
                            "(sid, server_id, instance_type, os) VALUES "
                            "(UNHEX('{sid}'), UNHEX('{server_id}'), '{instance_type}', {os})"
                    ).format(
                            sid=sid.hex,
                            server_id=uuid.UUID(managed['server_id']).hex,
                            instance_type=managed['instance_type'],
                            os=os_map[managed['os']])
                    app.analytics_db.execute(query, retries=1)

                # not_managed
                if region_data['not_managed']:
                    base_query = (
                            "INSERT IGNORE INTO notmanaged "
                            "(sid, instance_id, instance_type, os) VALUES %s")
                    values_template = "(UNHEX('{sid}'), '{instance_id}', '{instance_type}', {os})"
                    i, chunk_size = 0, 20
                    while True:
                        chunk_not_managed = region_data['not_managed'][i * chunk_size:(i + 1) * chunk_size]
                        if not chunk_not_managed:
                            break
                        query = base_query % ','.join(
                            [
                                values_template.format(
                                    sid=sid.hex,
                                    instance_id=_['instance_id'],
                                    instance_type=_['instance_type'],
                                    os=os_map[_['os']]
                                )
                                for _ in chunk_not_managed
                            ]
                        )
                        app.analytics_db.execute(query, retries=1)
                        i += 1
            except:
                msg = 'Database update failed, reason: {0}'
                msg = msg.format(helper.exc_info())
                LOG.warning(msg)



def process_credential(cred, envs_ids=None):
    if envs_ids is None:
        envs_ids = [cred.env_id]

    try:
        cloud_data = eval(cred.platform)(cred)
        if cloud_data:
            sorted_data = sort_nodes(cloud_data, cred, envs_ids)
            sorted_data_update(sorted_data)
            db_update(sorted_data, envs_ids, cred)
    except:
        msg = 'platform: {platform}, environments: {envs}, reason: {error}'
        msg = msg.format(platform=cred.platform, envs=envs_ids, error=helper.exc_info())
        LOG.error(msg)



class AnalyticsPoller(application.ScalrIterationApplication):

    def __init__(self, argv=None):
        self.description = "Scalr Cost Analytics poller application"

        super(AnalyticsPoller, self).__init__(argv=argv)

        self.config['connections'].update({
            'analytics': {
                'user': None,
                'pass': None,
                'host': None,
                'port': 3306,
                'name': None,
                'pool_size': 25,
            },
        })
        self.config.update({
            'pool_size': 100,
            'interval': 300,
            'cloud_connection_timeout': 10
        })

        self.scalr_db = None
        self.analytics_db = None
        self.analytics = None
        self.pool = None
        self.crypto_key = None


    def configure(self):
        enabled = self.scalr_config.get('analytics', {}).get('enabled', False)
        if not enabled:
            sys.stdout.write('Analytics is disabled. Exit\n')
            sys.exit(0)
        helper.update_config(
                self.scalr_config.get('analytics', {}).get('connections', {}).get('scalr', {}),
                self.config['connections']['mysql'])
        helper.update_config(
                self.scalr_config.get('analytics', {}).get('connections', {}).get('analytics', {}),
                self.config['connections']['analytics'])
        helper.update_config(
                self.scalr_config.get('analytics', {}).get('poller', {}),
                self.config)
        helper.validate_config(self.config)

        self.config['pool_size'] = max(11, self.config['pool_size'])
        self.iteration_timeout = self.config['interval'] - 5

        crypto_key_path = os.path.join(self.scalr_dir, 'app/etc/.cryptokey')
        self.crypto_key = cryptotool.read_key(crypto_key_path)
        self.scalr_db = dbmanager.ScalrDB(self.config['connections']['mysql'])
        self.analytics_db = dbmanager.ScalrDB(self.config['connections']['analytics'])
        self.analytics = analytics.Analytics(self.scalr_db, self.analytics_db)
        self.pool = helper.GPool(pool_size=self.config['pool_size'])

        socket.setdefaulttimeout(self.config['instances_connection_timeout'])


    def do_iteration(self):
        for envs in self.analytics.load_envs(limit=500):
            creds = self.analytics.load_creds(envs, platforms)
            unique_creds = self.analytics.filter_creds(creds)
            for _ in unique_creds:
                envs_ids = _['env_id']
                cred = _['cred']
                while len(self.pool) > self.config['pool_size'] * 5 / 10:
                    gevent.sleep(0.1)
                self.pool.apply_async(
                    process_credential,
                    args=(cred,), kwds={'envs_ids': envs_ids})
                gevent.sleep(0)  # force switch
        self.pool.join()

    def on_iteration_error(self):
        self.pool.kill()



def main():
    global app
    app = AnalyticsPoller()
    try:
        app.load_config()
        app.configure()
        app.run()
    except exceptions.AlreadyRunningError:
        LOG.info(helper.exc_info(where=False))
    except (SystemExit, KeyboardInterrupt):
        pass
    except:
        LOG.exception('Oops')



if __name__ == '__main__':
    main()

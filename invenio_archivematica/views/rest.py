# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2017 CERN.
#
# Invenio is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Invenio is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place, Suite 330, Boston,
# MA 02111-1307, USA.
#
# In applying this license, CERN does not
# waive the privileges and immunities granted to it by virtue of its status
# as an Intergovernmental Organization or submit itself to any jurisdiction.

"""Invenio-Archivematica REST API views."""

from functools import wraps

import requests
from flask import Blueprint, Response, abort, current_app, jsonify, \
    make_response, stream_with_context
from invenio_access.permissions import Permission
from invenio_db import db
from invenio_oauth2server import require_api_auth, require_oauth_scopes
from invenio_rest import ContentNegotiatedMethodView
from requests.exceptions import ConnectionError
from webargs import fields
from webargs.flaskparser import use_kwargs
from werkzeug.datastructures import Headers

from invenio_archivematica.api import change_status_func
from invenio_archivematica.models import Archive as Archive_
from invenio_archivematica.models import ArchiveStatus, status_converter
from invenio_archivematica.permissions import _action2need_map
from invenio_archivematica.scopes import archive_scope

blueprint = Blueprint(
    'invenio_archivematica_api',
    __name__,
    url_prefix="/oais"
)


def pass_accession_id(f):
    """Decorate to retrieve an Archive object."""
    @wraps(f)
    def decorate(*args, **kwargs):
        accession_id = kwargs.pop('accession_id')
        archive = Archive_.get_from_accession_id(accession_id)
        if not archive:
            abort(404, 'Accession_id {} not found.'.format(accession_id))
        return f(archive=archive, *args, **kwargs)
    return decorate


def check_permission(permission):
    """Decorate to check permission access.

    This decorator needs to be after pass_accession_id
    """
    def decorator(f):
        @wraps(f)
        def decoratee(*args, **kwargs):
            parameter = kwargs['archive'].accession_id
            perm = Permission(_action2need_map[permission](parameter))
            with perm.require(http_exception=403):
                return f(*args, **kwargs)
        return decoratee
    return decorator


def validate_status(status):
    """Accept only valid status."""
    try:
        status_converter(status)
    except Exception:
        return False
    return True


class Archive(ContentNegotiatedMethodView):
    """Status of the archival of a record."""

    def __init__(self, **kwargs):
        """Constructor."""
        kwargs['method_serializers'] = {
            'GET': {'application/json': make_response}
        }
        kwargs['default_method_media_type'] = {'GET': 'application/json'}
        kwargs['default_media_type'] = 'application/json'
        super(Archive, self).__init__(**kwargs)

    @staticmethod
    def _to_json(ark):
        """Return the archive as a JSON object.

        Used to return JSON as an answer.

        :param ark: the archive
        :type ark: :py:class:`invenio_archivematica.models.Archive`
        """
        return jsonify({
            'sip_id': ark.sip_id,
            'status': ark.status.value,
            'accession_id': ark.accession_id,
            'archivematica_id': ark.archivematica_id
        })

    @require_api_auth()
    @require_oauth_scopes(archive_scope.id)
    @pass_accession_id
    @check_permission('archive-write')
    @use_kwargs({
        'status': fields.Str(
            load_from='status',
            required=False,
            location='json',
            validate=validate_status
        ),
        'archivematica_id': fields.Str(
            load_from='archivematica_id',
            required=False,
            location='json'
        )
    })
    def patch(self, archive, status='', archivematica_id=''):
        """Change an Archive object.

        The accesion_id is used to change the object. You can only change
        the status or the archivematica_id.
        """
        if archivematica_id and archivematica_id != archive.archivematica_id:
            archive.archivematica_id = archivematica_id
        db.session.commit()
        ark_status = status_converter(status)
        if ark_status and ark_status != archive.status:
            change_status_func[ark_status](archive.sip, archive.accession_id,
                                           archive.archivematica_id)
        return self._to_json(archive)

    @require_api_auth()
    @require_oauth_scopes(archive_scope.id)
    @pass_accession_id
    @check_permission('archive-read')
    @use_kwargs({
        'real_status': fields.Boolean(
            load_from='realStatus',
            required=False,
            location='json'
        )
    })
    def get(self, archive, real_status=False):
        """Returns the status of the Archive object.

        :param bool real_status: If real_status is True, ask Archivematica to
            get the current status, as it may be delayed in Invenio servers.
            As we are requesting the Archivematica server, this adds an
            overload rarely welcomed... Thus, this parameter should be False
            most of the time.
        :return: a JSON object representing the archive.
        :rtype: str
        """
        if not real_status \
                or archive.status == ArchiveStatus.FAILED \
                or archive.status == ArchiveStatus.NEW \
                or not archive.archivematica_id:
            return self._to_json(archive)
        # we ask Archivematica
        # first we look at the status of the transfer
        if archive.status in (ArchiveStatus.WAITING,
                              ArchiveStatus.PROCESSING_TRANSFER):
            url = '{base}/api/transfer/status/{uuid}/'.format(
                base=current_app.config['ARCHIVEMATICA_DASHBOARD_URL'],
                uuid=archive.archivematica_id)
            params = {
                'username': current_app.config['ARCHIVEMATICA_DASHBOARD_USER'],
                'api_key':
                    current_app.config['ARCHIVEMATICA_DASHBOARD_API_KEY']
            }
            response = requests.get(url, params=params)
            if not response.ok:  # a problem occured
                return jsonify({}), response.status_code
            status = status_converter(response.json()['status'])
            # if status is not complete, we stop here
            if status != ArchiveStatus.REGISTERED:
                if status != archive.status:
                    change_status_func[status](archive.sip,
                                               archive.accession_id,
                                               archive.archivematica_id)
                return self._to_json(archive)
            # transfer finished, we need to update the archive
            status = ArchiveStatus.PROCESSING_AIP
            archive.archivematica_id = response.json()['sip_uuid']
            change_status_func[status](archive.sip, archive.accession_id,
                                       archive.archivematica_id)
        # we try to get the status of the SIP
        url = '{base}/api/ingest/status/{uuid}/'.format(
            base=current_app.config['ARCHIVEMATICA_DASHBOARD_URL'],
            uuid=archive.archivematica_id)
        response = requests.get(url, params=params)
        if not response.ok:  # a problem occured
            return jsonify({}), response.status_code
        status = status_converter(response.json()['status'],
                                  aip_processing=True)
        if status != archive.status:
            change_status_func[status](archive.sip, archive.accession_id,
                                       archive.archivematica_id)
        return self._to_json(archive)


class ArchiveDownload(ContentNegotiatedMethodView):
    """Stream file from Archivematica."""

    @require_api_auth()
    @require_oauth_scopes(archive_scope.id)
    @pass_accession_id
#    @check_permission('archive-read')
    def get(self, archive):
        """Send the archive object as a file to the client.

        :return: a file taken from archivematica.
        """
        if not archive.status == ArchiveStatus.REGISTERED \
                or not archive.archivematica_id:
            return make_response('Archive has not been registered yet.', 412)
        try:
            url = '{base}/api/v2/file/{uuid}/download/'.format(
                base=current_app.config['ARCHIVEMATICA_STORAGE_URL'],
                uuid=archive.archivematica_id)
            params = {
                'username': current_app.config[
                    'ARCHIVEMATICA_STORAGE_USER'],
                'api_key': current_app.config[
                    'ARCHIVEMATICA_STORAGE_API_KEY']
            }
            response = requests.get(url, params=params, stream=True)
            if response.ok:
                headers = Headers()
                for key, value in response.headers.items():
                    headers.add(key, value)
                return Response(stream_with_context(
                    response.iter_content(chunk_size=10*1024)),
                    content_type='application/octet-stream',
                    headers=headers)
            # problem
            return make_response('', response.status_code)
        except ConnectionError as e:
            return make_response('Connection problem with Archivematica', 520)


blueprint.add_url_rule(
    '/archive/<string:accession_id>/',
    view_func=Archive.as_view('archive_api')
)

blueprint.add_url_rule(
    '/archive/<string:accession_id>/download/',
    view_func=ArchiveDownload.as_view('download_api')
)

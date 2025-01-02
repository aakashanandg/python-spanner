# Copyright 2016 Google LLC All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Context manager for Cloud Spanner batched writes."""
import functools

from google.cloud.spanner_v1 import CommitRequest
from google.cloud.spanner_v1 import Mutation
from google.cloud.spanner_v1 import TransactionOptions
from google.cloud.spanner_v1 import BatchWriteRequest

from google.cloud.spanner_v1._helpers import _SessionWrapper
from google.cloud.spanner_v1._helpers import _make_list_value_pbs
from google.cloud.spanner_v1._helpers import (
    _metadata_with_prefix,
    _metadata_with_leader_aware_routing,
)
from google.cloud.spanner_v1._opentelemetry_tracing import trace_call
from google.cloud.spanner_v1 import RequestOptions
from google.cloud.spanner_v1._helpers import _retry
from google.cloud.spanner_v1._helpers import _check_rst_stream_error
from google.api_core.exceptions import InternalServerError
from google.api_core.exceptions import Aborted
import time

DEFAULT_RETRY_TIMEOUT_SECS = 30


class _BatchBase(_SessionWrapper):
    """Accumulate mutations for transmission during :meth:`commit`.

    :type session: :class:`~google.cloud.spanner_v1.session.Session`
    :param session: the session used to perform the commit
    """

    transaction_tag = None
    _read_only = False

    def __init__(self, session):
        super(_BatchBase, self).__init__(session)
        self._mutations = []

    def _check_state(self):
        """Helper for :meth:`commit` et al.

        Subclasses must override

        :raises: :exc:`ValueError` if the object's state is invalid for making
                 API requests.
        """
        raise NotImplementedError

    def insert(self, table, columns, values):
        """Insert one or more new table rows.

        :type table: str
        :param table: Name of the table to be modified.

        :type columns: list of str
        :param columns: Name of the table columns to be modified.

        :type values: list of lists
        :param values: Values to be modified.
        """
        self._mutations.append(Mutation(insert=_make_write_pb(table, columns, values)))

    def update(self, table, columns, values):
        """Update one or more existing table rows.

        :type table: str
        :param table: Name of the table to be modified.

        :type columns: list of str
        :param columns: Name of the table columns to be modified.

        :type values: list of lists
        :param values: Values to be modified.
        """
        self._mutations.append(Mutation(update=_make_write_pb(table, columns, values)))

    def insert_or_update(self, table, columns, values):
        """Insert/update one or more table rows.

        :type table: str
        :param table: Name of the table to be modified.

        :type columns: list of str
        :param columns: Name of the table columns to be modified.

        :type values: list of lists
        :param values: Values to be modified.
        """
        self._mutations.append(
            Mutation(insert_or_update=_make_write_pb(table, columns, values))
        )

    def replace(self, table, columns, values):
        """Replace one or more table rows.

        :type table: str
        :param table: Name of the table to be modified.

        :type columns: list of str
        :param columns: Name of the table columns to be modified.

        :type values: list of lists
        :param values: Values to be modified.
        """
        self._mutations.append(Mutation(replace=_make_write_pb(table, columns, values)))

    def delete(self, table, keyset):
        """Delete one or more table rows.

        :type table: str
        :param table: Name of the table to be modified.

        :type keyset: :class:`~google.cloud.spanner_v1.keyset.Keyset`
        :param keyset: Keys/ranges identifying rows to delete.
        """
        delete = Mutation.Delete(table=table, key_set=keyset._to_pb())
        self._mutations.append(Mutation(delete=delete))


class Batch(_BatchBase):
    """Accumulate mutations for transmission during :meth:`commit`."""

    committed = None
    commit_stats = None
    """Timestamp at which the batch was successfully committed."""

    def _check_state(self):
        """Helper for :meth:`commit` et al.

        Subclasses must override

        :raises: :exc:`ValueError` if the object's state is invalid for making
                 API requests.
        """
        if self.committed is not None:
            raise ValueError("Batch already committed")

    def commit(
        self,
        return_commit_stats=False,
        request_options=None,
        max_commit_delay=None,
        exclude_txn_from_change_streams=False,
        **kwargs
    ):
        """Commit mutations to the database.

        :type return_commit_stats: bool
        :param return_commit_stats:
          If true, the response will return commit stats which can be accessed though commit_stats.

        :type request_options:
            :class:`google.cloud.spanner_v1.types.RequestOptions`
        :param request_options:
                (Optional) Common options for this request.
                If a dict is provided, it must be of the same form as the protobuf
                message :class:`~google.cloud.spanner_v1.types.RequestOptions`.

        :type max_commit_delay: :class:`datetime.timedelta`
        :param max_commit_delay:
                (Optional) The amount of latency this request is willing to incur
                in order to improve throughput.

        :rtype: datetime
        :returns: timestamp of the committed changes.
        """
        self._check_state()
        database = self._session._database
        api = database.spanner_api
        metadata = _metadata_with_prefix(database.name)
        if database._route_to_leader_enabled:
            metadata.append(
                _metadata_with_leader_aware_routing(database._route_to_leader_enabled)
            )
        txn_options = TransactionOptions(
            read_write=TransactionOptions.ReadWrite(),
            exclude_txn_from_change_streams=exclude_txn_from_change_streams,
        )
        trace_attributes = {"num_mutations": len(self._mutations)}

        if request_options is None:
            request_options = RequestOptions()
        elif type(request_options) is dict:
            request_options = RequestOptions(request_options)
        request_options.transaction_tag = self.transaction_tag

        # Request tags are not supported for commit requests.
        request_options.request_tag = None

        request = CommitRequest(
            session=self._session.name,
            mutations=self._mutations,
            single_use_transaction=txn_options,
            return_commit_stats=return_commit_stats,
            max_commit_delay=max_commit_delay,
            request_options=request_options,
        )
        observability_options = getattr(database, "observability_options", None)
        with trace_call(
            "CloudSpanner.Commit",
            self._session,
            trace_attributes,
            observability_options=observability_options,
        ):
            method = functools.partial(
                api.commit,
                request=request,
                metadata=metadata,
            )
            deadline = time.time() + kwargs.get(
                "timeout_secs", DEFAULT_RETRY_TIMEOUT_SECS
            )
            response = _retry(
                method,
                allowed_exceptions={
                    InternalServerError: _check_rst_stream_error,
                    Aborted: no_op_handler,
                },
                deadline=deadline,
            )
        self.committed = response.commit_timestamp
        self.commit_stats = response.commit_stats
        return self.committed

    def __enter__(self):
        """Begin ``with`` block."""
        self._check_state()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """End ``with`` block."""
        if exc_type is None:
            self.commit()


class MutationGroup(_BatchBase):
    """A container for mutations.

    Clients should use :class:`~google.cloud.spanner_v1.MutationGroups` to
    obtain instances instead of directly creating instances.

    :type session: :class:`~google.cloud.spanner_v1.session.Session`
    :param session: The session used to perform the commit.

    :type mutations: list
    :param mutations: The list into which mutations are to be accumulated.
    """

    def __init__(self, session, mutations=[]):
        super(MutationGroup, self).__init__(session)
        self._mutations = mutations


class MutationGroups(_SessionWrapper):
    """Accumulate mutation groups for transmission during :meth:`batch_write`.

    :type session: :class:`~google.cloud.spanner_v1.session.Session`
    :param session: the session used to perform the commit
    """

    committed = None

    def __init__(self, session):
        super(MutationGroups, self).__init__(session)
        self._mutation_groups = []

    def _check_state(self):
        """Checks if the object's state is valid for making API requests.

        :raises: :exc:`ValueError` if the object's state is invalid for making
                 API requests.
        """
        if self.committed is not None:
            raise ValueError("MutationGroups already committed")

    def group(self):
        """Returns a new `MutationGroup` to which mutations can be added."""
        mutation_group = BatchWriteRequest.MutationGroup()
        self._mutation_groups.append(mutation_group)
        return MutationGroup(self._session, mutation_group.mutations)

    def batch_write(
        self, request_options=None, exclude_txn_from_change_streams=False, **kwargs
    ):
        """Executes batch_write.

        :type request_options:
            :class:`google.cloud.spanner_v1.types.RequestOptions`
        :param request_options:
                (Optional) Common options for this request.
                If a dict is provided, it must be of the same form as the protobuf
                message :class:`~google.cloud.spanner_v1.types.RequestOptions`.

        :type exclude_txn_from_change_streams: bool
        :param exclude_txn_from_change_streams:
          (Optional) If true, instructs the transaction to be excluded from being recorded in change streams
          with the DDL option `allow_txn_exclusion=true`. This does not exclude the transaction from
          being recorded in the change streams with the DDL option `allow_txn_exclusion` being false or
          unset.

        :rtype: :class:`Iterable[google.cloud.spanner_v1.types.BatchWriteResponse]`
        :returns: a sequence of responses for each batch.
        """
        self._check_state()

        database = self._session._database
        api = database.spanner_api
        metadata = _metadata_with_prefix(database.name)
        if database._route_to_leader_enabled:
            metadata.append(
                _metadata_with_leader_aware_routing(database._route_to_leader_enabled)
            )
        trace_attributes = {"num_mutation_groups": len(self._mutation_groups)}
        if request_options is None:
            request_options = RequestOptions()
        elif type(request_options) is dict:
            request_options = RequestOptions(request_options)

        request = BatchWriteRequest(
            session=self._session.name,
            mutation_groups=self._mutation_groups,
            request_options=request_options,
            exclude_txn_from_change_streams=exclude_txn_from_change_streams,
        )
        observability_options = getattr(database, "observability_options", None)
        with trace_call(
            "CloudSpanner.BatchWrite",
            self._session,
            trace_attributes,
            observability_options=observability_options,
        ):
            method = functools.partial(
                api.batch_write,
                request=request,
                metadata=metadata,
            )
            deadline = time.time() + kwargs.get(
                "timeout_secs", DEFAULT_RETRY_TIMEOUT_SECS
            )
            response = _retry(
                method,
                allowed_exceptions={
                    InternalServerError: _check_rst_stream_error,
                    Aborted: no_op_handler,
                },
                deadline=deadline,
            )
        self.committed = True
        return response


def _make_write_pb(table, columns, values):
    """Helper for :meth:`Batch.insert` et al.

    :type table: str
    :param table: Name of the table to be modified.

    :type columns: list of str
    :param columns: Name of the table columns to be modified.

    :type values: list of lists
    :param values: Values to be modified.

    :rtype: :class:`google.cloud.spanner_v1.types.Mutation.Write`
    :returns: Write protobuf
    """
    return Mutation.Write(
        table=table, columns=columns, values=_make_list_value_pbs(values)
    )


def no_op_handler(exc):
    # No-op (does nothing)
    pass

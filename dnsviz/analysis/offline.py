#
# This file is a part of DNSViz, a tool suite for DNS/DNSSEC monitoring,
# analysis, and visualization.  This file (or some portion thereof) is a
# derivative work authored by VeriSign, Inc., and created in 2014, based on
# code originally developed at Sandia National Laboratories.
# Created by Casey Deccio (casey@deccio.net)
#
# Copyright 2012-2014 Sandia Corporation. Under the terms of Contract
# DE-AC04-94AL85000 with Sandia Corporation, the U.S. Government retains
# certain rights in this software.
#
# Copyright 2014-2015 VeriSign, Inc.
#
# DNSViz is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# DNSViz is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import collections
import errno
import logging

import dns.flags, dns.rdataclass, dns.rdatatype

from dnsviz import crypto
import dnsviz.format as fmt
import dnsviz.query as Q
from dnsviz import response as Response
from dnsviz.util import tuple_to_dict

import errors as Errors
from online import OnlineDomainNameAnalysis, \
        ANALYSIS_TYPE_AUTHORITATIVE, ANALYSIS_TYPE_RECURSIVE, ANALYSIS_TYPE_CACHE
import status as Status

DNS_PROCESSED_VERSION = '1.0'

_logger = logging.getLogger(__name__)

class FoundYXDOMAIN(Exception):
    pass

class OfflineDomainNameAnalysis(OnlineDomainNameAnalysis):
    RDTYPES_ALL = 0
    RDTYPES_ALL_SAME_NAME = 1
    RDTYPES_NS_TARGET = 2
    RDTYPES_SECURE_DELEGATION = 3
    RDTYPES_DELEGATION = 4

    QUERY_CLASS = Q.TTLDistinguishingMultQueryAggregateDNSResponse

    def __init__(self, *args, **kwargs):
        super(OfflineDomainNameAnalysis, self).__init__(*args, **kwargs)

        if self.analysis_type != ANALYSIS_TYPE_AUTHORITATIVE:
            self._query_cls = Q.MultiQueryAggregateDNSResponse

        # Shortcuts to the values in the SOA record.
        self.serial = None
        self.rname = None
        self.mname = None

        self.dnssec_algorithms_in_dnskey = set()
        self.dnssec_algorithms_in_ds = set()
        self.dnssec_algorithms_in_dlv = set()
        self.dnssec_algorithms_digest_in_ds = set()
        self.dnssec_algorithms_digest_in_dlv = set()

        self.status = None
        self.yxdomain = None
        self.yxrrset = None
        self.nxrrset = None
        self.rrset_warnings = None
        self.rrset_errors = None
        self.rrsig_status = None
        self.response_component_status = None
        self.wildcard_status = None
        self.dname_status = None
        self.nxdomain_status = None
        self.nxdomain_warnings = None
        self.nxdomain_errors = None
        self.nodata_status = None
        self.nodata_warnings = None
        self.nodata_errors = None
        self.response_errors = None

        self.ds_status_by_ds = None
        self.ds_status_by_dnskey = None

        self.delegation_warnings = None
        self.delegation_errors = None
        self.delegation_status = None

        self.published_keys = None
        self.revoked_keys = None
        self.zsks = None
        self.ksks = None
        self.dnskey_with_ds = None

    def _signed(self):
        return bool(self.dnssec_algorithms_in_dnskey or self.dnssec_algorithms_in_ds or self.dnssec_algorithms_in_dlv)
    signed = property(_signed)

    def _handle_soa_response(self, rrset):
        '''Indicate that there exists an SOA record for the name which is the
        subject of this analysis, and save the relevant parts.'''

        self.has_soa = True
        if self.serial is None or rrset[0].serial > self.serial:
            self.serial = rrset[0].serial
            self.rname = rrset[0].rname
            self.mname = rrset[0].mname

    def _handle_dnskey_response(self, rrset):
        for dnskey in rrset:
            self.dnssec_algorithms_in_dnskey.add(dnskey.algorithm)

    def _handle_ds_response(self, rrset):
        if rrset.rdtype == dns.rdatatype.DS:
            dnssec_algs = self.dnssec_algorithms_in_ds
            digest_algs = self.dnssec_algorithms_digest_in_ds
        else:
            dnssec_algs = self.dnssec_algorithms_in_dlv
            digest_algs = self.dnssec_algorithms_digest_in_dlv
        for ds in rrset:
            dnssec_algs.add(ds.algorithm)
            digest_algs.add((ds.algorithm, ds.digest_type))

    def _process_response_answer_rrset(self, rrset, query, response):
        super(OfflineDomainNameAnalysis, self)._process_response_answer_rrset(rrset, query, response)
        if query.qname in (self.name, self.dlv_name):
            if rrset.rdtype == dns.rdatatype.SOA:
                self._handle_soa_response(rrset)
            elif rrset.rdtype == dns.rdatatype.DNSKEY:
                self._handle_dnskey_response(rrset)
            elif rrset.rdtype in (dns.rdatatype.DS, dns.rdatatype.DLV):
                self._handle_ds_response(rrset)

    def _index_dnskeys(self):
        self._dnskey_sets = []
        self._dnskeys = {}
        if (self.name, dns.rdatatype.DNSKEY) not in self.queries:
            return
        for dnskey_info in self.queries[(self.name, dns.rdatatype.DNSKEY)].answer_info:
            # there are CNAMEs that show up here...
            if not (dnskey_info.rrset.name == self.name and dnskey_info.rrset.rdtype == dns.rdatatype.DNSKEY):
                continue
            dnskey_set = set()
            for dnskey_rdata in dnskey_info.rrset:
                if dnskey_rdata not in self._dnskeys:
                    self._dnskeys[dnskey_rdata] = Response.DNSKEYMeta(dnskey_info.rrset.name, dnskey_rdata, dnskey_info.rrset.ttl)
                self._dnskeys[dnskey_rdata].rrset_info.append(dnskey_info)
                self._dnskeys[dnskey_rdata].servers_clients.update(dnskey_info.servers_clients)
                dnskey_set.add(self._dnskeys[dnskey_rdata])

            self._dnskey_sets.append((dnskey_set, dnskey_info))

    def get_dnskey_sets(self):
        if not hasattr(self, '_dnskey_sets') or self._dnskey_sets is None:
            self._index_dnskeys()
        return self._dnskey_sets

    def get_dnskeys(self):
        if not hasattr(self, '_dnskeys') or self._dnskeys is None:
            self._index_dnskeys()
        return self._dnskeys.values()

    def potential_trusted_keys(self):
        active_ksks = self.ksks.difference(self.zsks).difference(self.revoked_keys)
        if active_ksks:
            return active_ksks
        return self.ksks.difference(self.revoked_keys)

    def _serialize_nsec_set_simple(self, nsec_set_info, neg_status, response_info):
        nsec_tup = []
        if neg_status[nsec_set_info]:
            for nsec_status in neg_status[nsec_set_info]:
                # assign the "overall" status of the NSEC proof, based on both
                # the correctness of the NSEC proof as well as the
                # authentication status of the collective records comprising
                # the proof.
                #
                # if the proof is not valid, then use the validity status of
                # the proof as the overall status.
                if nsec_status.validation_status != Status.NSEC_STATUS_VALID:
                    status = Status.nsec_status_mapping[nsec_status.validation_status]
                # else (the NSEC proof is valid)
                else:
                    # if there is a component status, then set the overall
                    # status to the authentication status of collective records
                    # comprising the proof (the proof is only as good as it is
                    # authenticated).
                    if self.response_component_status is not None:
                        status = Status.rrset_status_mapping[self.response_component_status[nsec_status.nsec_set_info]]
                    # otherwise, set the overall status to insecure
                    else:
                        status = Status.rrset_status_mapping[Status.RRSET_STATUS_INSECURE]

                warnings = [w.code for w in nsec_status.warnings]
                errors = [e.code for e in nsec_status.errors]
                nsec_tup.append(('PROOF', status, [], [], [(Status.nsec_status_mapping[nsec_status.validation_status], warnings, errors, '')]))

                for nsec_rrset_info in nsec_status.nsec_set_info.rrsets.values():
                    nsec_tup.extend(self._serialize_response_component_simple(nsec_rrset_info.rrset.rdtype, response_info, nsec_rrset_info, True))

        return nsec_tup

    def _serialize_rrsig_simple(self, name_obj, rrset_info):
        rrsig_tup = []
        if name_obj.rrsig_status[rrset_info]:
            rrsigs = name_obj.rrsig_status[rrset_info].keys()
            rrsigs.sort()
            for rrsig in rrsigs:
                dnskeys = name_obj.rrsig_status[rrset_info][rrsig].keys()
                dnskeys.sort()
                for dnskey in dnskeys:
                    rrsig_status = name_obj.rrsig_status[rrset_info][rrsig][dnskey]

                    # assign the "overall" status of the RRSIG, based on both
                    # the validity of the RRSIG as well as the authentication
                    # status of the DNSKEY with which it is validated
                    #
                    # if the RRSIG is not valid, then use the RRSIG status as
                    # the overall status
                    if rrsig_status.validation_status != Status.RRSIG_STATUS_VALID:
                        status = Status.rrsig_status_mapping[rrsig_status.validation_status]
                    # else (the status of the RRSIG is valid)
                    else:
                        # if there is a component status, then set the overall
                        # status to that of the status of the DNSKEY (an RRSIG
                        # is only as authentic as the DNSKEY that signs it)
                        if self.response_component_status is not None:
                            status = Status.rrset_status_mapping[self.response_component_status[dnskey]]
                        # otherwise, set the overall status to insecure
                        else:
                            status = Status.rrset_status_mapping[Status.RRSET_STATUS_INSECURE]

                    warnings = [w.code for w in rrsig_status.warnings]
                    errors = [e.code for e in rrsig_status.errors]
                    rrsig_tup.append(('RRSIG', status, [], [], [(Status.rrsig_status_mapping[rrsig_status.validation_status], warnings, errors, '%s/%s/%s (%s - %s)' % \
                            (fmt.humanize_name(rrsig.signer), rrsig.algorithm, rrsig.key_tag, fmt.timestamp_to_str(rrsig.inception)[:10], fmt.timestamp_to_str(rrsig.expiration)[:10]))]))
        return rrsig_tup

    def _serialize_response_component_simple(self, rdtype, response_info, info, show_neg_response, dname_status=None):
        tup = []
        rdata = []
        if isinstance(info, Errors.DomainNameAnalysisError):
            status = 'ERROR'
        else:
            if self.response_component_status is not None:
                status = Status.rrset_status_mapping[self.response_component_status[info]]
            else:
                status = Status.rrset_status_mapping[Status.RRSET_STATUS_INSECURE]

        rdata_tup = []
        rrsig_tup = []
        if isinstance(info, Response.RRsetInfo):
            if info.rrset.rdtype == dns.rdatatype.CNAME:
                rdata_tup.append((None, [], [], 'CNAME %s' % (info.rrset[0].target.to_text())))
            elif rdtype == dns.rdatatype.DNSKEY:
                for d in info.rrset:
                    dnskey_meta = response_info.name_obj._dnskeys[d]
                    warnings = [w.code for w in dnskey_meta.warnings]
                    errors = [e.code for e in dnskey_meta.errors]
                    rdata_tup.append(('VALID', warnings, errors, '%d/%d/%d' % (d.algorithm, dnskey_meta.key_tag, d.flags)))
            elif rdtype == dns.rdatatype.DS:
                dss = response_info.name_obj.ds_status_by_ds[dns.rdatatype.DS].keys()
                dss.sort()
                for ds in dss:
                    # only show the DS if in the RRset in question
                    if ds not in info.rrset:
                        continue
                    dnskeys = response_info.name_obj.ds_status_by_ds[rdtype][ds].keys()
                    dnskeys.sort()
                    for dnskey in dnskeys:
                        ds_status = response_info.name_obj.ds_status_by_ds[rdtype][ds][dnskey]
                        warnings = [w.code for w in ds_status.warnings]
                        errors = [e.code for e in ds_status.errors]
                        rdata_tup.append((Status.ds_status_mapping[ds_status.validation_status], warnings, errors, '%d/%d/%d' % (ds.algorithm, ds.key_tag, ds.digest_type)))
            elif rdtype == dns.rdatatype.NSEC3:
                rdata_tup.append((None, [], [], '%s %s' % (fmt.format_nsec3_name(info.rrset.name), fmt.format_nsec3_rrset_text(info.rrset[0].to_text()))))
            elif rdtype == dns.rdatatype.NSEC:
                rdata_tup.append((None, [], [], '%s %s' % (info.rrset.name.to_text(), info.rrset[0].to_text())))
            elif rdtype == dns.rdatatype.DNAME:
                warnings = [w.code for w in dname_status.warnings]
                errors = [e.code for e in dname_status.errors]
                rdata_tup.append((Status.dname_status_mapping[dname_status.validation_status], warnings, errors, info.rrset[0].to_text()))
            else:
                rdata_tup.extend([(None, [], [], r.to_text()) for r in info.rrset])

            warnings = [w.code for w in response_info.name_obj.rrset_warnings[info]]
            errors = [e.code for e in response_info.name_obj.rrset_errors[info]]

            rrsig_tup = self._serialize_rrsig_simple(response_info.name_obj, info)
            for wildcard_name in info.wildcard_info:
                rrsig_tup.extend(self._serialize_nsec_set_simple(info.wildcard_info[wildcard_name], response_info.name_obj.wildcard_status, response_info))

            if info in response_info.name_obj.dname_status:
                for dname_status in response_info.name_obj.dname_status[info]:
                    rrsig_tup.extend(self._serialize_response_component_simple(dns.rdatatype.DNAME, response_info, dname_status.synthesized_cname.dname_info, True, dname_status))

        elif isinstance(info, Errors.DomainNameAnalysisError):
            warnings = []
            errors = []
            rdata_tup.append((None, [], [], 'ERROR %s' % (info.code)))
        elif info in self.nodata_status:
            warnings = [w.code for w in response_info.name_obj.nodata_warnings[info]]
            errors = [e.code for e in response_info.name_obj.nodata_errors[info]]

            if not self.nodata_status[info] and not show_neg_response:
                return []
            rdata_tup.append((None, [], [], 'NODATA'))
            for soa_rrset_info in info.soa_rrset_info:
                rrsig_tup.extend(self._serialize_response_component_simple(dns.rdatatype.SOA, response_info, soa_rrset_info, True))
            rrsig_tup.extend(self._serialize_nsec_set_simple(info, response_info.name_obj.nodata_status, response_info))

        elif info in self.nxdomain_status:
            warnings = [w.code for w in response_info.name_obj.nxdomain_warnings[info]]
            errors = [e.code for e in response_info.name_obj.nxdomain_errors[info]]

            if not self.nxdomain_status[info] and not show_neg_response:
                return []
            rdata_tup.append((None, [], [], 'NXDOMAIN'))
            for soa_rrset_info in info.soa_rrset_info:
                rrsig_tup.extend(self._serialize_response_component_simple(dns.rdatatype.SOA, response_info, soa_rrset_info, True))
            rrsig_tup.extend(self._serialize_nsec_set_simple(info, response_info.name_obj.nxdomain_status, response_info))

        tup.append((dns.rdatatype.to_text(rdtype), status, warnings, errors, rdata_tup))
        tup.extend(rrsig_tup)
        return tup

    def _serialize_response_component_list_simple(self, rdtype, response_info, show_neg_response):
        tup = []
        for info, cname_chain_info in response_info.response_info_list:
            tup.extend(self._serialize_response_component_simple(rdtype, response_info, info, show_neg_response))
        return tup

    def _serialize_status_simple(self, response_info_list, processed):
        tup = []
        cname_info_map = collections.OrderedDict()

        # just get the first one since the names are all supposed to be the
        # same
        response_info = response_info_list[0]

        # first build the ancestry in reverse order
        ancestry = []
        parent_obj = response_info.zone_obj
        while parent_obj is not None:
            ancestry.insert(0, parent_obj)
            parent_obj = parent_obj.parent

        name_tup = None

        # now process the DS and DNSKEY for each name in the ancestry
        for parent_obj in ancestry:
            if (parent_obj.name, -1) in processed:
                continue
            processed.add((parent_obj.name, -1))

            if parent_obj.stub:
                continue

            status = None
            warnings = []
            errors = []
            dnskey_response_info = parent_obj.get_response_info(parent_obj.name, dns.rdatatype.DNSKEY)
            if parent_obj.parent is not None:
                ds_response_info = parent_obj.get_response_info(parent_obj.name, dns.rdatatype.DS)
                if parent_obj.is_zone():
                    status = Status.delegation_status_mapping[parent_obj.delegation_status[dns.rdatatype.DS]]
                    warnings = [w.code for w in parent_obj.delegation_warnings[dns.rdatatype.DS]]
                    errors = [e.code for e in parent_obj.delegation_errors[dns.rdatatype.DS]]
            else:
                ds_response_info = None

            name_tup = (fmt.humanize_name(parent_obj.name), status, warnings, errors, [])
            tup.append(name_tup)

            if ds_response_info is not None:
                name_tup[4].extend(parent_obj._serialize_response_component_list_simple(dns.rdatatype.DS, ds_response_info, False))

            # if we only care about DS for the name itself, then don't
            # serialize the DNSKEY response
            if response_info.rdtype == dns.rdatatype.DS and parent_obj.name == response_info.qname:
                pass
            else:
                name_tup[4].extend(parent_obj._serialize_response_component_list_simple(dns.rdatatype.DNSKEY, dnskey_response_info, False))

            parent_is_signed = parent_obj.signed

        # in recursive analysis, if we don't contact any servers that are
        # valid and responsive, then we get a zone_obj (and thus
        # parent_obj, in this case) that is None (because we couldn't
        # detect any NS records in the ancestry)
        #
        # in this case, or in the case where the name is not a zone (and
        # thus changes), we create a new tuple.
        if parent_obj is None or response_info.qname != parent_obj.name or name_tup is None:
            name_tup = (fmt.humanize_name(response_info.qname), None, [], [], [])
            tup.append(name_tup)

        for response_info in response_info_list:
            if (response_info.qname, response_info.rdtype) in processed:
                continue
            processed.add((response_info.qname, response_info.rdtype))

            # if we've already done this one (above) then just move along.
            # These were only done if the name is a zone.
            if response_info.name_obj.is_zone() and \
                    response_info.rdtype in (dns.rdatatype.DNSKEY, dns.rdatatype.DS):
                continue

            name_tup[4].extend(response_info.name_obj._serialize_response_component_list_simple(response_info.rdtype, response_info, True))

            # queue the cnames for later serialization
            for info, cname_info in response_info.response_info_list:
                if cname_info is None:
                    continue
                if cname_info.qname not in cname_info_map:
                    cname_info_map[cname_info.qname] = []
                cname_info_map[cname_info.qname].append(cname_info)

        # now serialize the cnames
        for qname in cname_info_map:
            tup.extend(self._serialize_status_simple(cname_info_map[qname], processed))

        return tup

    def serialize_status_simple(self, rdtypes=None, processed=None):
        if processed is None:
            processed = set()

        response_info_map = {}
        for qname, rdtype in self.queries:
            if rdtypes is None:
                # if rdtypes was not specified, then serialize all, with some exceptions
                if rdtype in (dns.rdatatype.NS, dns.rdatatype.DNSKEY, dns.rdatatype.DS, dns.rdatatype.DLV):
                    continue
            else:
                # if rdtypes was specified, then only serialize rdtypes that
                # were specified
                if qname != self.name or rdtype not in rdtypes:
                    continue
            if qname not in response_info_map:
                response_info_map[qname] = {}
            response_info_map[qname][rdtype] = self.get_response_info(qname, rdtype)

        tuples = []
        qnames = response_info_map.keys()
        qnames.sort()
        for qname in qnames:
            rdtypes = response_info_map[qname].keys()
            rdtypes.sort()
            response_info_list = [response_info_map[qname][r] for r in rdtypes]
            tuples.extend(self._serialize_status_simple(response_info_list, processed))

        return tuples

    def _rdtypes_for_analysis_level(self, level):
        rdtypes = set([self.referral_rdtype, dns.rdatatype.NS])
        if level == self.RDTYPES_DELEGATION:
            return rdtypes
        rdtypes.update([dns.rdatatype.DNSKEY, dns.rdatatype.DS, dns.rdatatype.DLV])
        if level == self.RDTYPES_SECURE_DELEGATION:
            return rdtypes
        rdtypes.update([dns.rdatatype.A, dns.rdatatype.AAAA])
        if level == self.RDTYPES_NS_TARGET:
            return rdtypes
        return None

    def _server_responsive_with_condition(self, server, client, tcp, response_test):
        for query in self.queries.values():
            for query1 in query.queries.values():
                try:
                    if client is None:
                        clients = query1.responses[server].keys()
                    else:
                        clients = (client,)
                except KeyError:
                    continue

                for c in clients:
                    try:
                        response = query1.responses[server][client]
                    except KeyError:
                        continue
                    # if tcp is specified, then only follow through if the
                    # query was ultimately issued according to that value
                    if tcp is not None:
                        if tcp and not response.effective_tcp:
                            continue
                        if not tcp and response.effective_tcp:
                            continue
                    if response_test(response):
                        return True
        return False

    def server_responsive_with_edns_flag(self, server, client, tcp, f):
        return self._server_responsive_with_condition(server, client, tcp,
                lambda x: ((x.effective_tcp and x.tcp_responsive) or \
                        (not x.effective_tcp and x.udp_responsive)) and \
                        x.effective_edns >= 0 and x.effective_edns_flags & f)

    def server_responsive_valid_with_edns_flag(self, server, client, tcp, f):
        return self._server_responsive_with_condition(server, client, tcp,
                lambda x: ((x.effective_tcp and x.tcp_responsive) or \
                        (not x.effective_tcp and x.udp_responsive)) and \
                        x.is_valid_response() and \
                        x.effective_edns >= 0 and x.effective_edns_flags & f)

    def server_responsive_with_do(self, server, client, tcp):
        return self.server_responsive_with_edns_flag(server, client, tcp, dns.flags.DO)

    def server_responsive_valid_with_do(self, server, client, tcp):
        return self.server_responsive_valid_with_edns_flag(server, client, tcp, dns.flags.DO)

    def server_responsive_with_edns(self, server, client, tcp):
        return self._server_responsive_with_condition(server, client, tcp,
                lambda x: ((x.effective_tcp and x.tcp_responsive) or \
                        (not x.effective_tcp and x.udp_responsive)) and \
                        x.effective_edns >= 0)

    def server_responsive_valid_with_edns(self, server, client, tcp):
        return self._server_responsive_with_condition(server, client, tcp,
                lambda x: ((x.effective_tcp and x.tcp_responsive) or \
                        (not x.effective_tcp and x.udp_responsive)) and \
                        x.is_valid_response() and \
                        x.effective_edns >= 0)

    def populate_status(self, trusted_keys, supported_algs=None, supported_digest_algs=None, is_dlv=False, trace=None, follow_mx=True):
        if trace is None:
            trace = []

        # avoid loops
        if self in trace:
            self._populate_name_status()
            return

        # if status has already been populated, then don't reevaluate
        if self.rrsig_status is not None:
            return

        # if we're a stub, there's nothing to evaluate
        if self.stub:
            return

        # identify supported algorithms as intersection of explicitly supported
        # and software supported
        if supported_algs is not None:
            supported_algs.intersection_update(crypto._supported_algs)
        else:
            supported_algs = crypto._supported_algs
        if supported_digest_algs is not None:
            supported_digest_algs.intersection_update(crypto._supported_digest_algs)
        else:
            supported_digest_algs = crypto._supported_digest_algs

        # populate status of dependencies
        for cname in self.cname_targets:
            for target, cname_obj in self.cname_targets[cname].items():
                if cname_obj is not None:
                    cname_obj.populate_status(trusted_keys, trace=trace + [self])
        if follow_mx:
            for target, mx_obj in self.mx_targets.items():
                if mx_obj is not None:
                    mx_obj.populate_status(trusted_keys, trace=trace + [self], follow_mx=False)
        for signer, signer_obj in self.external_signers.items():
            if signer_obj is not None:
                signer_obj.populate_status(trusted_keys, trace=trace + [self])
        for target, ns_obj in self.ns_dependencies.items():
            if ns_obj is not None:
                ns_obj.populate_status(trusted_keys, trace=trace + [self])

        # populate status of ancestry
        if self.parent is not None:
            self.parent.populate_status(trusted_keys, supported_algs, supported_digest_algs, trace=trace + [self])
        if self.dlv_parent is not None:
            self.dlv_parent.populate_status(trusted_keys, supported_algs, supported_digest_algs, is_dlv=True, trace=trace + [self])

        _logger.debug('Assessing status of %s...' % (fmt.humanize_name(self.name)))
        self._populate_name_status()
        self._index_dnskeys()
        self._populate_rrsig_status_all(supported_algs)
        self._populate_nodata_status(supported_algs)
        self._populate_nxdomain_status(supported_algs)
        self._finalize_key_roles()
        if not is_dlv:
            self._populate_delegation_status(supported_algs, supported_digest_algs)
        if self.dlv_parent is not None:
            self._populate_ds_status(dns.rdatatype.DLV, supported_algs, supported_digest_algs)
        self._populate_dnskey_status(trusted_keys)

    def _populate_name_status(self, trace=None):
        # using trace allows _populate_name_status to be called independent of
        # populate_status
        if trace is None:
            trace = []

        # avoid loops
        if self in trace:
            return

        self.status = Status.NAME_STATUS_INDETERMINATE
        self.yxdomain = set()
        self.yxrrset = set()
        self.nxrrset = set()

        bailiwick_map, default_bailiwick = self.get_bailiwick_mapping()

        for (qname, rdtype), query in self.queries.items():

            qname_obj = self.get_name(qname)
            if rdtype == dns.rdatatype.DS:
                qname_obj = qname_obj.parent
            elif rdtype == dns.rdatatype.DLV and qname == qname_obj.dlv_name:
                qname_obj = qname_obj.dlv_parent

            for rrset_info in query.answer_info:
                self.yxdomain.add(rrset_info.rrset.name)
                self.yxrrset.add((rrset_info.rrset.name, rrset_info.rrset.rdtype))
                if rrset_info.dname_info is not None:
                    self.yxrrset.add((rrset_info.dname_info.rrset.name, rrset_info.dname_info.rrset.rdtype))
                for cname_rrset_info in rrset_info.cname_info_from_dname:
                    self.yxrrset.add((cname_rrset_info.dname_info.rrset.name, cname_rrset_info.dname_info.rrset.rdtype))
                    self.yxrrset.add((cname_rrset_info.rrset.name, cname_rrset_info.rrset.rdtype))
            for neg_response_info in query.nodata_info:
                for (server,client) in neg_response_info.servers_clients:
                    for response in neg_response_info.servers_clients[(server,client)]:
                        if neg_response_info.qname == qname or response.recursion_desired_and_available():
                            if not response.is_upward_referral(qname_obj.zone.name):
                                self.yxdomain.add(neg_response_info.qname)
                            self.nxrrset.add((neg_response_info.qname, neg_response_info.rdtype))
            for neg_response_info in query.nxdomain_info:
                for (server,client) in neg_response_info.servers_clients:
                    for response in neg_response_info.servers_clients[(server,client)]:
                        if neg_response_info.qname == qname or response.recursion_desired_and_available():
                            self.nxrrset.add((neg_response_info.qname, neg_response_info.rdtype))

            # now check referrals (if name hasn't already been identified as YXDOMAIN)
            if self.name == qname and self.name not in self.yxdomain:
                if rdtype not in (self.referral_rdtype, dns.rdatatype.NS):
                    continue
                try:
                    for query1 in query.queries.values():
                        for server in query1.responses:
                            bailiwick = bailiwick_map.get(server, default_bailiwick)
                            for client in query1.responses[server]:
                                if query1.responses[server][client].is_referral(self.name, rdtype, bailiwick, proper=True):
                                    self.yxdomain.add(self.name)
                                    raise FoundYXDOMAIN
                except FoundYXDOMAIN:
                    pass

        # now add the values of CNAMEs
        for cname in self.cname_targets:
            for target, cname_obj in self.cname_targets[cname].items():
                if cname_obj is self:
                    continue
                if cname_obj is None:
                    continue
                if cname_obj.yxrrset is None:
                    cname_obj._populate_name_status(trace=trace + [self])
                for name, rdtype in cname_obj.yxrrset:
                    if name == target:
                        self.yxrrset.add((cname,rdtype))

        if self.name in self.yxdomain:
            self.status = Status.NAME_STATUS_NOERROR

        if self.status == Status.NAME_STATUS_INDETERMINATE:
            for (qname, rdtype), query in self.queries.items():
                if rdtype == dns.rdatatype.DS:
                    continue
                if filter(lambda x: x.qname == qname, query.nxdomain_info):
                    self.status = Status.NAME_STATUS_NXDOMAIN
                    break

    def _populate_response_errors(self, qname_obj, response, server, client, warnings, errors):
        # if the initial request used EDNS
        if response.query.edns >= 0:
            err = None
            #TODO check for general intermittent errors (i.e., not just for EDNS/DO)
            #TODO mark a slow response as well (over a certain threshold)

            # if the response didn't use EDNS
            if response.message.edns < 0:
                # if the effective request didn't use EDNS either
                if response.effective_edns < 0:
                    # find out what made us turn off EDNS to elicit a valid response
                    if response.responsive_cause_index is not None:
                        # there was some type of a network error with EDNS in use
                        if response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_NETWORK_ERROR:
                            if qname_obj is not None and qname_obj.zone.server_responsive_with_edns(server,client,response.responsive_cause_index_tcp):
                                query_specific = True
                            else:
                                query_specific = False
                            err = Errors.ResponseErrorWithEDNS(response_error=Errors.NetworkError(tcp=response.responsive_cause_index_tcp, errno=errno.errorcode.get(response.history[response.responsive_cause_index].cause_arg, 'UNKNOWN')), query_specific=query_specific)
                        # there was a malformed response with EDNS in use
                        elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_FORMERR:
                            if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns(server,client,response.responsive_cause_index_tcp):
                                query_specific = True
                            else:
                                query_specific = False
                            err = Errors.ResponseErrorWithEDNS(response_error=Errors.FormError(tcp=response.responsive_cause_index_tcp, msg_size=response.msg_size), query_specific=query_specific)
                        # the response timed out with EDNS in use
                        elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_TIMEOUT:
                            if qname_obj is not None and qname_obj.zone.server_responsive_with_edns(server,client,response.responsive_cause_index_tcp):
                                query_specific = True
                            else:
                                query_specific = False
                            err = Errors.ResponseErrorWithEDNS(response_error=Errors.Timeout(tcp=response.responsive_cause_index_tcp, attempts=response.responsive_cause_index+1), query_specific=query_specific)
                        # the RCODE was something other than NOERROR or NXDOMAIN
                        elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_RCODE:
                            # if the RCODE was FORMERR, SERVFAIL, or NOTIMP,
                            # then this is a legitimate reason for falling back
                            if response.history[response.responsive_cause_index].cause_arg in (dns.rcode.FORMERR, dns.rcode.SERVFAIL, dns.rcode.NOTIMP):
                                pass
                            # the RCODE was invalid with EDNS
                            else:
                                if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns(server,client,response.responsive_cause_index_tcp):
                                    query_specific = True
                                else:
                                    query_specific = False
                                err = Errors.ResponseErrorWithEDNS(response_error=Errors.InvalidRcode(tcp=response.responsive_cause_index_tcp, rcode=dns.rcode.to_text(response.history[response.responsive_cause_index].cause_arg)), query_specific=query_specific)
                        # any other errors
                        elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_OTHER:
                            if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns(server,client,response.responsive_cause_index_tcp):
                                query_specific = True
                            else:
                                query_specific = False
                            err = Errors.ResponseErrorWithEDNS(response_error=Errors.UnknownResponseError(tcp=response.responsive_cause_index_tcp), query_specific=query_specific)

                        #XXX is there another (future) reason why  we would
                        # have disabled EDNS?
                        else:
                            pass

                    # if EDNS was disabled in the request, but the response was
                    # still bad (indicated by the lack of a value for
                    # responsive_cause_index), then don't report this as an
                    # EDNS error
                    else:
                        pass

                # if the ultimate request used EDNS, then it was simply ignored
                # by the server
                else:
                    err = Errors.EDNSIgnored()

                ##TODO handle this better
                #if err is None and response.responsive_cause_index is not None:
                #    raise Exception('Unknown EDNS-related error')

            # the response did use EDNS
            else:

                # check for EDNS version mismatch
                if response.message.edns != response.query.edns:
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.UnsupportedEDNSVersion(version=response.query.edns), warnings, server, client, response)

                # check for PMTU issues
                #TODO need bounding here
                if response.effective_edns_max_udp_payload != response.query.edns_max_udp_payload:
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.PMTUExceeded(pmtu_lower_bound=None, pmtu_upper_bound=None), warnings, server, client, response)

                if response.query.edns_flags != response.effective_edns_flags:
                    for i in range(15, -1, -1):
                        f = 1 << i
                        # the response used EDNS with the given flag, but the flag
                        # wasn't (ultimately) requested
                        if ((response.query.edns_flags & f) != (response.effective_edns_flags & f)):
                            # find out if this really appears to be a flag issue,
                            # by seeing if any other queries to this server with
                            # the specified flag were also unsuccessful
                            if response.responsive_cause_index is not None:
                                if response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_NETWORK_ERROR:
                                    if qname_obj is not None and qname_obj.zone.server_responsive_with_edns_flag(server,client,response.responsive_cause_index_tcp,f):
                                        query_specific = True
                                    else:
                                        query_specific = False
                                    err = Errors.ResponseErrorWithEDNSFlag(response_error=Errors.NetworkError(tcp=response.responsive_cause_index_tcp, errno=errno.errorcode.get(response.history[response.responsive_cause_index].cause_arg, 'UNKNOWN')), query_specific=query_specific, flag=dns.flags.edns_to_text(f))
                                elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_FORMERR:
                                    if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns_flag(server,client,response.responsive_cause_index_tcp,f):
                                        query_specific = True
                                    else:
                                        query_specific = False
                                    err = Errors.ResponseErrorWithEDNSFlag(response_error=Errors.FormError(tcp=response.responsive_cause_index_tcp, msg_size=response.msg_size), query_specific=query_specific, flag=dns.flags.edns_to_text(f))
                                elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_TIMEOUT:
                                    if qname_obj is not None and qname_obj.zone.server_responsive_with_edns_flag(server,client,response.responsive_cause_index_tcp,f):
                                        query_specific = True
                                    else:
                                        query_specific = False
                                    err = Errors.ResponseErrorWithEDNSFlag(response_error=Errors.Timeout(tcp=response.responsive_cause_index_tcp, attempts=response.responsive_cause_index+1), query_specific=query_specific, flag=dns.flags.edns_to_text(f))
                                elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_OTHER:
                                    if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns_flag(server,client,response.responsive_cause_index_tcp,f):
                                        query_specific = True
                                    else:
                                        query_specific = False
                                    err = Errors.ResponseErrorWithEDNSFlag(response_error=Errors.UnknownResponseError(tcp=response.responsive_cause_index_tcp), query_specific=query_specific, flag=dns.flags.edns_to_text(f))
                                elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_RCODE:
                                    if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns_flag(server,client,response.responsive_cause_index_tcp,f):
                                        query_specific = True
                                    else:
                                        query_specific = False
                                    err = Errors.ResponseErrorWithEDNSFlag(response_error=Errors.InvalidRcode(tcp=response.responsive_cause_index_tcp, rcode=dns.rcode.to_text(response.history[response.responsive_cause_index].cause_arg)), query_specific=query_specific, flag=dns.flags.edns_to_text(f))

                                #XXX is there another (future) reason why we would
                                # have disabled an EDNS flag?
                                else:
                                    pass

                            # if an EDNS flag was disabled in the request,
                            # but the response was still bad (indicated by
                            # the lack of a value for
                            # responsive_cause_index), then don't report
                            # this as an EDNS flag error
                            else:
                                pass

                        if err is not None:
                            break

                    #TODO handle this better
                    if err is None and response.responsive_cause_index is not None:
                        raise Exception('Unknown EDNS-flag-related error')

            if err is not None:
                # warn on intermittent errors
                if isinstance(err, Errors.InvalidResponseError):
                    group = warnings
                # if the error really matters (e.g., due to DNSSEC), note an error
                elif qname_obj is not None and qname_obj.zone.signed:
                    group = errors
                # otherwise, warn
                else:
                    group = warnings

                Errors.DomainNameAnalysisError.insert_into_list(err, group, server, client, response)

        if qname_obj is not None:
            if qname_obj.analysis_type == ANALYSIS_TYPE_AUTHORITATIVE:
                if not response.is_authoritative():
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.NotAuthoritative(), errors, server, client, response)
            elif qname_obj.analysis_type == ANALYSIS_TYPE_RECURSIVE:
                if response.recursion_desired() and not response.recursion_available():
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.RecursionNotAvailable(), errors, server, client, response)

    def _populate_wildcard_status(self, query, rrset_info, qname_obj, supported_algs):
        for wildcard_name in rrset_info.wildcard_info:
            if qname_obj is None:
                zone_name = wildcard_info.parent()
            else:
                zone_name = qname_obj.zone.name

            servers_missing_nsec = set()
            for server, client in rrset_info.wildcard_info[wildcard_name].servers_clients:
                for response in rrset_info.wildcard_info[wildcard_name].servers_clients[(server,client)]:
                    servers_missing_nsec.add((server,client,response))

            statuses = []
            status_by_response = {}
            for nsec_set_info in rrset_info.wildcard_info[wildcard_name].nsec_set_info:
                if nsec_set_info.use_nsec3:
                    status = Status.NSEC3StatusWildcard(rrset_info.rrset.name, wildcard_name, rrset_info.rrset.rdtype, zone_name, nsec_set_info)
                else:
                    status = Status.NSECStatusWildcard(rrset_info.rrset.name, wildcard_name, rrset_info.rrset.rdtype, zone_name, nsec_set_info)

                for nsec_rrset_info in nsec_set_info.rrsets.values():
                    self._populate_rrsig_status(query, nsec_rrset_info, qname_obj, supported_algs)

                if status.validation_status == Status.NSEC_STATUS_VALID:
                    if status not in statuses:
                        statuses.append(status)

                for server, client in nsec_set_info.servers_clients:
                    for response in nsec_set_info.servers_clients[(server,client)]:
                        if (server,client,response) in servers_missing_nsec:
                            servers_missing_nsec.remove((server,client,response))
                        if status.validation_status == Status.NSEC_STATUS_VALID:
                            if (server,client,response) in status_by_response:
                                del status_by_response[(server,client,response)]
                        else:
                            status_by_response[(server,client,response)] = status

            for (server,client,response), status in status_by_response.items():
                if status not in statuses:
                    statuses.append(status)

            self.wildcard_status[rrset_info.wildcard_info[wildcard_name]] = statuses

            for server, client, response in servers_missing_nsec:
                # by definition, DNSSEC was requested (otherwise we
                # wouldn't know this was a wildcard), so no need to
                # check for DO bit in request
                Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingNSECForWildcard(), self.rrset_errors[rrset_info], server, client, response)

    def _initialize_rrset_status(self, rrset_info):
        self.rrset_warnings[rrset_info] = []
        self.rrset_errors[rrset_info] = []
        self.rrsig_status[rrset_info] = {}

    def _populate_rrsig_status(self, query, rrset_info, qname_obj, supported_algs, populate_response_errors=True):
        self._initialize_rrset_status(rrset_info)

        if qname_obj is None:
            zone_name = None
        else:
            zone_name = qname_obj.zone.name

        if qname_obj is None:
            dnssec_algorithms_in_dnskey = set()
            dnssec_algorithms_in_ds = set()
            dnssec_algorithms_in_dlv = set()
        else:
            dnssec_algorithms_in_dnskey = qname_obj.zone.dnssec_algorithms_in_dnskey
            if query.rdtype == dns.rdatatype.DLV:
                dnssec_algorithms_in_ds = set()
                dnssec_algorithms_in_dlv = set()
            else:
                dnssec_algorithms_in_ds = qname_obj.zone.dnssec_algorithms_in_ds
                dnssec_algorithms_in_dlv = qname_obj.zone.dnssec_algorithms_in_dlv

        # handle DNAMEs
        has_dname = set()
        if rrset_info.rrset.rdtype == dns.rdatatype.CNAME:
            if rrset_info.dname_info is not None:
                dname_info_list = [rrset_info.dname_info]
                dname_status = Status.CNAMEFromDNAMEStatus(rrset_info, None)
            elif rrset_info.cname_info_from_dname:
                dname_info_list = [c.dname_info for c in rrset_info.cname_info_from_dname]
                dname_status = Status.CNAMEFromDNAMEStatus(rrset_info.cname_info_from_dname[0], rrset_info)
            else:
                dname_info_list = []
                dname_status = None

            if dname_info_list:
                for dname_info in dname_info_list:
                    for server, client in dname_info.servers_clients:
                        has_dname.update([(server,client,response) for response in dname_info.servers_clients[(server,client)]])

                if rrset_info not in self.dname_status:
                    self.dname_status[rrset_info] = []
                self.dname_status[rrset_info].append(dname_status)

        algs_signing_rrset = {}
        if dnssec_algorithms_in_dnskey or dnssec_algorithms_in_ds or dnssec_algorithms_in_dlv:
            for server, client in rrset_info.servers_clients:
                for response in rrset_info.servers_clients[(server, client)]:
                    if (server, client, response) not in has_dname:
                        algs_signing_rrset[(server, client, response)] = set()

        for rrsig in rrset_info.rrsig_info:
            self.rrsig_status[rrset_info][rrsig] = {}

            signer = self.get_name(rrsig.signer)

            #XXX
            if signer is not None:

                if signer.stub:
                    continue

                for server, client in rrset_info.rrsig_info[rrsig].servers_clients:
                    for response in rrset_info.rrsig_info[rrsig].servers_clients[(server,client)]:
                        if (server,client,response) not in algs_signing_rrset:
                            continue
                        algs_signing_rrset[(server,client,response)].add(rrsig.algorithm)
                        if not dnssec_algorithms_in_dnskey.difference(algs_signing_rrset[(server,client,response)]) and \
                                not dnssec_algorithms_in_ds.difference(algs_signing_rrset[(server,client,response)]) and \
                                not dnssec_algorithms_in_dlv.difference(algs_signing_rrset[(server,client,response)]):
                            del algs_signing_rrset[(server,client,response)]

                # define self-signature
                self_sig = rrset_info.rrset.rdtype == dns.rdatatype.DNSKEY and rrsig.signer == rrset_info.rrset.name

                checked_keys = set()
                for dnskey_set, dnskey_meta in signer.get_dnskey_sets():
                    validation_status_mapping = { True: set(), False: set(), None: set() }
                    for dnskey in dnskey_set:
                        # if we've already checked this key (i.e., in
                        # another DNSKEY RRset) then continue
                        if dnskey in checked_keys:
                            continue
                        # if this is a RRSIG over DNSKEY RRset, then make sure we're validating
                        # with a DNSKEY that is actually in the set
                        if self_sig and dnskey.rdata not in rrset_info.rrset:
                            continue
                        checked_keys.add(dnskey)
                        if not (dnskey.rdata.protocol == 3 and \
                                rrsig.key_tag in (dnskey.key_tag, dnskey.key_tag_no_revoke) and \
                                rrsig.algorithm == dnskey.rdata.algorithm):
                            continue
                        rrsig_status = Status.RRSIGStatus(rrset_info, rrsig, dnskey, zone_name, fmt.datetime_to_timestamp(self.analysis_end), supported_algs)
                        validation_status_mapping[rrsig_status.signature_valid].add(rrsig_status)

                    # if we got results for multiple keys, then just select the one that validates
                    for status in True, False, None:
                        if validation_status_mapping[status]:
                            for rrsig_status in validation_status_mapping[status]:
                                self.rrsig_status[rrsig_status.rrset][rrsig_status.rrsig][rrsig_status.dnskey] = rrsig_status

                                if self.is_zone() and rrset_info.rrset.name == self.name and \
                                        rrset_info.rrset.rdtype != dns.rdatatype.DS and \
                                        rrsig_status.dnskey is not None:
                                    if rrset_info.rrset.rdtype == dns.rdatatype.DNSKEY:
                                        self.ksks.add(rrsig_status.dnskey)
                                    else:
                                        self.zsks.add(rrsig_status.dnskey)

                                key = rrsig_status.rrset, rrsig_status.rrsig
                            break

            # no corresponding DNSKEY
            if not self.rrsig_status[rrset_info][rrsig]:
                rrsig_status = Status.RRSIGStatus(rrset_info, rrsig, None, self.zone.name, fmt.datetime_to_timestamp(self.analysis_end), supported_algs)
                self.rrsig_status[rrsig_status.rrset][rrsig_status.rrsig][None] = rrsig_status

        # list errors for rrsets with which no RRSIGs were returned or not all algorithms were accounted for
        for server,client,response in algs_signing_rrset:
            errors = self.rrset_errors[rrset_info]
            # report an error if all RRSIGs are missing
            if not algs_signing_rrset[(server,client,response)]:
                if response.dnssec_requested():
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingRRSIG(), errors, server, client, response)
                elif qname_obj is not None and qname_obj.zone.server_responsive_with_do(server,client,response.effective_tcp):
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.UnableToRetrieveDNSSECRecords(), errors, server, client, response)
            else:
                # report an error if RRSIGs for one or more algorithms are missing
                for alg in dnssec_algorithms_in_dnskey.difference(algs_signing_rrset[(server,client,response)]):
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingRRSIGForAlgDNSKEY(algorithm=alg), errors, server, client, response)
                for alg in dnssec_algorithms_in_ds.difference(algs_signing_rrset[(server,client,response)]):
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingRRSIGForAlgDS(algorithm=alg), errors, server, client, response)
                for alg in dnssec_algorithms_in_dlv.difference(algs_signing_rrset[(server,client,response)]):
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingRRSIGForAlgDLV(algorithm=alg), errors, server, client, response)

        self._populate_wildcard_status(query, rrset_info, qname_obj, supported_algs)

        if populate_response_errors:
            for server,client in rrset_info.servers_clients:
                for response in rrset_info.servers_clients[(server,client)]:
                    self._populate_response_errors(qname_obj, response, server, client, self.rrset_warnings[rrset_info], self.rrset_errors[rrset_info])

    def _populate_invalid_response_status(self, query):
        self.response_errors[query] = []
        for error_info in query.error_info:
            for server, client in error_info.servers_clients:
                for response in error_info.servers_clients[(server, client)]:
                    if error_info.code == Q.RESPONSE_ERROR_NETWORK_ERROR:
                        Errors.DomainNameAnalysisError.insert_into_list(Errors.NetworkError(tcp=response.effective_tcp, errno=errno.errorcode.get(error_info.arg, 'UNKNOWN')), self.response_errors[query], server, client, response)
                    if error_info.code == Q.RESPONSE_ERROR_FORMERR:
                        Errors.DomainNameAnalysisError.insert_into_list(Errors.FormError(tcp=response.effective_tcp, msg_size=response.msg_size), self.response_errors[query], server, client, response)
                    elif error_info.code == Q.RESPONSE_ERROR_TIMEOUT:
                        attempts = 1
                        for i in range(len(response.history) - 1, -1, -1):
                            if response.history[i].action in (Q.RETRY_ACTION_USE_TCP, Q.RETRY_ACTION_USE_UDP):
                                break
                            attempts += 1
                        Errors.DomainNameAnalysisError.insert_into_list(Errors.Timeout(tcp=response.effective_tcp, attempts=attempts), self.response_errors[query], server, client, response)
                    elif error_info.code == Q.RESPONSE_ERROR_INVALID_RCODE:
                        # if we used EDNS and didn't fall back, and the RCODE
                        # was FORMERR, SERVFAIL, or NOTIMP, then this is a
                        # legitimate reason for the RCODE
                        if response.effective_edns >= 0 and response.message.rcode() in (dns.rcode.FORMERR, dns.rcode.SERVFAIL, dns.rcode.NOTIMP):
                            pass
                        # if we used a non-zero version of EDNS and didn't fall
                        # back, and the RCODE was BADVERS, then this is a
                        # legitimate reason for the RCODE
                        elif response.effective_edns > 0 and response.message.rcode() == dns.rcode.BADVERS:
                            pass
                        else:
                            Errors.DomainNameAnalysisError.insert_into_list(Errors.InvalidRcode(tcp=response.effective_tcp, rcode=dns.rcode.to_text(response.message.rcode())), self.response_errors[query], server, client, response)
                    elif error_info.code == Q.RESPONSE_ERROR_OTHER:
                        Errors.DomainNameAnalysisError.insert_into_list(Errors.UnknownResponseError(tcp=response.effective_tcp), self.response_errors[query], server, client, response)

    def _populate_rrsig_status_all(self, supported_algs):
        self.rrset_warnings = {}
        self.rrset_errors = {}
        self.rrsig_status = {}
        self.dname_status = {}
        self.wildcard_status = {}
        self.response_errors = {}

        if self.is_zone():
            self.zsks = set()
            self.ksks = set()

        _logger.debug('Assessing RRSIG status of %s...' % (fmt.humanize_name(self.name)))
        for (qname, rdtype), query in self.queries.items():

            items_to_validate = []
            for rrset_info in query.answer_info:
                items_to_validate.append(rrset_info)
                if rrset_info.dname_info is not None:
                    items_to_validate.append(rrset_info.dname_info)
                for cname_rrset_info in rrset_info.cname_info_from_dname:
                    items_to_validate.append(cname_rrset_info.dname_info)
                    items_to_validate.append(cname_rrset_info)

            for rrset_info in items_to_validate:
                qname_obj = self.get_name(rrset_info.rrset.name)
                if rdtype == dns.rdatatype.DS:
                    qname_obj = qname_obj.parent
                elif rdtype == dns.rdatatype.DLV:
                    qname_obj = qname_obj.dlv_parent

                self._populate_rrsig_status(query, rrset_info, qname_obj, supported_algs)

            self._populate_invalid_response_status(query)

    def _finalize_key_roles(self):
        if self.is_zone():
            self.published_keys = set(self.get_dnskeys()).difference(self.zsks.union(self.ksks))
            self.revoked_keys = set(filter(lambda x: x.rdata.flags & fmt.DNSKEY_FLAGS['revoke'], self.get_dnskeys()))

    def _populate_ns_status(self, warn_no_ipv4=True, warn_no_ipv6=False):
        if not self.is_zone():
            return

        if self.parent is None:
            return

        if self.analysis_type != ANALYSIS_TYPE_AUTHORITATIVE:
            return

        all_names = self.get_ns_names()
        names_from_child = self.get_ns_names_in_child()
        names_from_parent = self.get_ns_names_in_parent()

        auth_ns_response = self.queries[(self.name, dns.rdatatype.NS)].is_valid_complete_authoritative_response_any()

        glue_mapping = self.get_glue_ip_mapping()
        auth_mapping = self.get_auth_ns_ip_mapping()

        ns_names_not_in_child = []
        ns_names_not_in_parent = []
        names_error_resolving = []
        names_with_glue_mismatch = []
        names_missing_glue = []
        names_missing_auth = []

        for name in all_names:
            # if name resolution resulted in an error (other than NXDOMAIN)
            if name not in auth_mapping:
                auth_addrs = set()
                names_error_resolving.append(name)
            else:
                auth_addrs = auth_mapping[name]
                # if name resolution completed successfully, but the response was
                # negative for both A and AAAA (NXDOMAIN or NODATA)
                if not auth_mapping[name]:
                    names_missing_auth.append(name)

            if names_from_parent:
                name_in_parent = name in names_from_parent
            elif self.delegation_status == Status.DELEGATION_STATUS_INCOMPLETE:
                name_in_parent = False
            else:
                name_in_parent = None

            if name_in_parent:
                # if glue is required and not supplied
                if name.is_subdomain(self.name) and not glue_mapping[name]:
                    names_missing_glue.append(name)

                # if glue is supplied, check that it matches the authoritative response
                if glue_mapping[name] and auth_addrs and glue_mapping[name] != auth_addrs:
                    names_with_glue_mismatch.append((name,glue_mapping[name],auth_addrs))

            elif name_in_parent is False:
                ns_names_not_in_parent.append(name)

            if name not in names_from_child and auth_ns_response:
                ns_names_not_in_child.append(name)

        if ns_names_not_in_child:
            ns_names_not_in_child.sort()
            self.delegation_warnings[dns.rdatatype.DS].append(Errors.NSNameNotInChild(names=map(lambda x: fmt.humanize_name(x), ns_names_not_in_child), parent=fmt.humanize_name(self.parent_name())))

        if ns_names_not_in_parent:
            ns_names_not_in_child.sort()
            self.delegation_warnings[dns.rdatatype.DS].append(Errors.NSNameNotInParent(names=map(lambda x: fmt.humanize_name(x), ns_names_not_in_parent), parent=fmt.humanize_name(self.parent_name())))

        if names_error_resolving:
            names_error_resolving.sort()
            self.delegation_errors[dns.rdatatype.DS].append(Errors.ErrorResolvingNSName(names=map(lambda x: fmt.humanize_name(x), names_error_resolving)))

        if names_with_glue_mismatch:
            names_with_glue_mismatch.sort()
            for name, glue_addrs, auth_addrs in names_with_glue_mismatch:
                glue_addrs = list(glue_addrs)
                glue_addrs.sort()
                auth_addrs = list(auth_addrs)
                auth_addrs.sort()
                self.delegation_warnings[dns.rdatatype.DS].append(Errors.GlueMismatchError(name=fmt.humanize_name(name), glue_addresses=glue_addrs, auth_addresses=auth_addrs))

        if names_missing_glue:
            names_missing_glue.sort()
            self.delegation_warnings[dns.rdatatype.DS].append(Errors.MissingGlueForNSName(names=map(lambda x: fmt.humanize_name(x), names_missing_glue)))

        if names_missing_auth:
            names_missing_auth.sort()
            self.delegation_errors[dns.rdatatype.DS].append(Errors.NoAddressForNSName(names=map(lambda x: fmt.humanize_name(x), names_missing_auth)))

        ips_from_parent = self.get_servers_in_parent()
        ips_from_parent_ipv4 = filter(lambda x: x.version == 4, ips_from_parent)
        ips_from_parent_ipv6 = filter(lambda x: x.version == 6, ips_from_parent)

        ips_from_child = self.get_servers_in_child()
        ips_from_child_ipv4 = filter(lambda x: x.version == 4, ips_from_child)
        ips_from_child_ipv6 = filter(lambda x: x.version == 6, ips_from_child)

        if not (ips_from_parent_ipv4 or ips_from_child_ipv4) and warn_no_ipv4:
            if ips_from_parent_ipv4:
                reference = 'child'
            elif ips_from_child_ipv4:
                reference = 'parent'
            else:
                reference = 'parent or child'
            self.delegation_warnings[dns.rdatatype.DS].append(Errors.NoNSAddressesForIPv4(reference=reference))

        if not (ips_from_parent_ipv6 or ips_from_child_ipv6) and warn_no_ipv6:
            if ips_from_parent_ipv6:
                reference = 'child'
            elif ips_from_child_ipv6:
                reference = 'parent'
            else:
                reference = 'parent or child'
            self.delegation_warnings[dns.rdatatype.DS].append(Errors.NoNSAddressesForIPv6(reference=reference))

    def _populate_delegation_status(self, supported_algs, supported_digest_algs):
        self.ds_status_by_ds = {}
        self.ds_status_by_dnskey = {}
        self.delegation_errors = {}
        self.delegation_warnings = {}
        self.delegation_status = {}
        self.dnskey_with_ds = set()

        self._populate_ds_status(dns.rdatatype.DS, supported_algs, supported_digest_algs)
        if self.dlv_parent is not None:
            self._populate_ds_status(dns.rdatatype.DLV, supported_algs, supported_digest_algs)
        self._populate_ns_status()
        self._populate_server_status()

    def _populate_ds_status(self, rdtype, supported_algs, supported_digest_algs):
        if rdtype not in (dns.rdatatype.DS, dns.rdatatype.DLV):
            raise ValueError('Type can only be DS or DLV.')
        if self.parent is None:
            return
        if rdtype == dns.rdatatype.DLV:
            name = self.dlv_name
            if name is None:
                raise ValueError('No DLV specified for DomainNameAnalysis object.')
        else:
            name = self.name

        _logger.debug('Assessing delegation status of %s...' % (fmt.humanize_name(self.name)))
        self.ds_status_by_ds[rdtype] = {}
        self.ds_status_by_dnskey[rdtype] = {}
        self.delegation_warnings[rdtype] = []
        self.delegation_errors[rdtype] = []
        self.delegation_status[rdtype] = None

        try:
            ds_rrset_answer_info = self.queries[(name, rdtype)].answer_info
        except KeyError:
            # zones should have DS queries
            if self.is_zone():
                raise
            else:
                return

        secure_path = False

        bailiwick_map, default_bailiwick = self.get_bailiwick_mapping()

        if (self.name, dns.rdatatype.DNSKEY) in self.queries:
            dnskey_multiquery = self.queries[(self.name, dns.rdatatype.DNSKEY)]
        else:
            dnskey_multiquery = self._query_cls(self.name, dns.rdatatype.DNSKEY, dns.rdataclass.IN)

        # populate all the servers queried for DNSKEYs to determine
        # what problems there were with regard to DS records and if
        # there is at least one match
        dnskey_server_client_responses = set()
        for dnskey_query in dnskey_multiquery.queries.values():
            for server in dnskey_query.responses:
                bailiwick = bailiwick_map.get(server, default_bailiwick)
                for client in dnskey_query.responses[server]:
                    response = dnskey_query.responses[server][client]
                    if response.is_valid_response() and response.is_complete_response() and not response.is_referral(self.name, dns.rdatatype.DNSKEY, bailiwick):
                        dnskey_server_client_responses.add((server,client,response))

        for ds_rrset_info in ds_rrset_answer_info:
            # there are CNAMEs that show up here...
            if not (ds_rrset_info.rrset.name == name and ds_rrset_info.rrset.rdtype == rdtype):
                continue

            # for each set of DS records provided by one or more servers,
            # identify the set of DNSSEC algorithms and the set of digest
            # algorithms per algorithm/key tag combination
            ds_algs = set()
            supported_ds_algs = set()
            for ds_rdata in ds_rrset_info.rrset:
                if ds_rdata.algorithm in supported_algs and ds_rdata.digest_type in supported_digest_algs:
                    supported_ds_algs.add(ds_rdata.algorithm)
                ds_algs.add(ds_rdata.algorithm)

            if supported_ds_algs:
                secure_path = True

            algs_signing_sep = {}
            algs_validating_sep = {}
            for server,client,response in dnskey_server_client_responses:
                algs_signing_sep[(server,client,response)] = set()
                algs_validating_sep[(server,client,response)] = set()

            for ds_rdata in ds_rrset_info.rrset:
                self.ds_status_by_ds[rdtype][ds_rdata] = {}

                for dnskey_info in dnskey_multiquery.answer_info:
                    # there are CNAMEs that show up here...
                    if not (dnskey_info.rrset.name == self.name and dnskey_info.rrset.rdtype == dns.rdatatype.DNSKEY):
                        continue

                    validation_status_mapping = { True: set(), False: set(), None: set() }
                    for dnskey_rdata in dnskey_info.rrset:
                        dnskey = self._dnskeys[dnskey_rdata]

                        if dnskey not in self.ds_status_by_dnskey[rdtype]:
                            self.ds_status_by_dnskey[rdtype][dnskey] = {}

                        # if the key tag doesn't match, then go any farther
                        if not (ds_rdata.key_tag in (dnskey.key_tag, dnskey.key_tag_no_revoke) and \
                                ds_rdata.algorithm == dnskey.rdata.algorithm):
                            continue

                        # check if the digest is a match
                        ds_status = Status.DSStatus(ds_rdata, ds_rrset_info, dnskey, supported_digest_algs)
                        validation_status_mapping[ds_status.digest_valid].add(ds_status)

                        # if dnskey exists, then add to dnskey_with_ds
                        if ds_status.validation_status not in \
                                (Status.DS_STATUS_INDETERMINATE_NO_DNSKEY, Status.DS_STATUS_INDETERMINATE_MATCH_PRE_REVOKE):
                            self.dnskey_with_ds.add(dnskey)

                        for rrsig in dnskey_info.rrsig_info:
                            # move along if DNSKEY is not self-signing
                            if dnskey not in self.rrsig_status[dnskey_info][rrsig]:
                                continue

                            # move along if key tag is not the same (i.e., revoke)
                            if dnskey.key_tag != rrsig.key_tag:
                                continue

                            for (server,client) in dnskey_info.rrsig_info[rrsig].servers_clients:
                                for response in dnskey_info.rrsig_info[rrsig].servers_clients[(server,client)]:
                                    if (server,client,response) in algs_signing_sep:
                                        # note that this algorithm is part of a self-signing DNSKEY
                                        algs_signing_sep[(server,client,response)].add(rrsig.algorithm)
                                        if not ds_algs.difference(algs_signing_sep[(server,client,response)]):
                                            del algs_signing_sep[(server,client,response)]

                                    if (server,client,response) in algs_validating_sep:
                                        # retrieve the status of the DNSKEY RRSIG
                                        rrsig_status = self.rrsig_status[dnskey_info][rrsig][dnskey]

                                        # if the DS digest and the RRSIG are both valid, and the digest algorithm
                                        # is not deprecated then mark it as a SEP
                                        if ds_status.validation_status == Status.DS_STATUS_VALID and \
                                                rrsig_status.validation_status == Status.RRSIG_STATUS_VALID:
                                            # note that this algorithm is part of a successful self-signing DNSKEY
                                            algs_validating_sep[(server,client,response)].add(rrsig.algorithm)
                                            if not ds_algs.difference(algs_validating_sep[(server,client,response)]):
                                                del algs_validating_sep[(server,client,response)]

                    # if we got results for multiple keys, then just select the one that validates
                    for status in True, False, None:
                        if validation_status_mapping[status]:
                            for ds_status in validation_status_mapping[status]:
                                self.ds_status_by_ds[rdtype][ds_status.ds][ds_status.dnskey] = ds_status
                                self.ds_status_by_dnskey[rdtype][ds_status.dnskey][ds_status.ds] = ds_status
                            break

                # no corresponding DNSKEY
                if not self.ds_status_by_ds[rdtype][ds_rdata]:
                    ds_status = Status.DSStatus(ds_rdata, ds_rrset_info, None, supported_digest_algs)
                    self.ds_status_by_ds[rdtype][ds_rdata][None] = ds_status
                    if None not in self.ds_status_by_dnskey[rdtype]:
                        self.ds_status_by_dnskey[rdtype][None] = {}
                    self.ds_status_by_dnskey[rdtype][None][ds_rdata] = ds_status

            if dnskey_server_client_responses:
                if not algs_validating_sep:
                    self.delegation_status[rdtype] = Status.DELEGATION_STATUS_SECURE
                else:
                    for server,client,response in dnskey_server_client_responses:
                        if (server,client,response) not in algs_validating_sep or \
                                supported_ds_algs.intersection(algs_validating_sep[(server,client,response)]):
                            self.delegation_status[rdtype] = Status.DELEGATION_STATUS_SECURE
                        elif supported_ds_algs:
                            Errors.DomainNameAnalysisError.insert_into_list(Errors.NoSEP(source=dns.rdatatype.to_text(rdtype)), self.delegation_errors[rdtype], server, client, response)

                # report an error if one or more algorithms are incorrectly validated
                for (server,client,response) in algs_signing_sep:
                    for alg in ds_algs.difference(algs_signing_sep[(server,client,response)]):
                        Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingSEPForAlg(algorithm=alg, source=dns.rdatatype.to_text(rdtype)), self.delegation_errors[rdtype], server, client, response)
            else:
                Errors.DomainNameAnalysisError.insert_into_list(Errors.NoSEP(source=dns.rdatatype.to_text(rdtype)), self.delegation_errors[rdtype], None, None, None)

        if self.delegation_status[rdtype] is None:
            if ds_rrset_answer_info:
                if secure_path:
                    self.delegation_status[rdtype] = Status.DELEGATION_STATUS_BOGUS
                else:
                    self.delegation_status[rdtype] = Status.DELEGATION_STATUS_INSECURE
            elif self.parent.signed:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_BOGUS
                for nsec_status_list in [self.nxdomain_status[n] for n in self.nxdomain_status if n.qname == name and n.rdtype == dns.rdatatype.DS] + \
                        [self.nodata_status[n] for n in self.nodata_status if n.qname == name and n.rdtype == dns.rdatatype.DS]:
                    for nsec_status in nsec_status_list:
                        if nsec_status.validation_status == Status.NSEC_STATUS_VALID:
                            self.delegation_status[rdtype] = Status.DELEGATION_STATUS_INSECURE
                            break
            else:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_INSECURE

        # if no servers (designated or stealth authoritative) respond or none
        # respond authoritatively, then make the delegation as lame
        if not self.get_auth_or_designated_servers():
            if self.delegation_status[rdtype] == Status.DELEGATION_STATUS_INSECURE:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_LAME
        elif not self.get_responsive_auth_or_designated_servers():
            if self.delegation_status[rdtype] == Status.DELEGATION_STATUS_INSECURE:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_LAME
        elif not self.get_valid_auth_or_designated_servers():
            if self.delegation_status[rdtype] == Status.DELEGATION_STATUS_INSECURE:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_LAME
        elif self.analysis_type == ANALYSIS_TYPE_AUTHORITATIVE and not self._auth_servers_clients:
            if self.delegation_status[rdtype] == Status.DELEGATION_STATUS_INSECURE:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_LAME

        if rdtype == dns.rdatatype.DS:
            try:
                ds_nxdomain_info = filter(lambda x: x.qname == name and x.rdtype == dns.rdatatype.DS, self.queries[(name, rdtype)].nxdomain_info)[0]
            except IndexError:
                pass
            else:
                err = Errors.NoNSInParent(parent=self.parent_name())
                err.servers_clients.update(ds_nxdomain_info.servers_clients)
                self.delegation_errors[rdtype].append(err)
                if self.delegation_status[rdtype] == Status.DELEGATION_STATUS_INSECURE:
                    self.delegation_status[rdtype] = Status.DELEGATION_STATUS_INCOMPLETE

    def _populate_server_status(self):
        if not self.is_zone():
            return

        if self.parent is None:
            return

        designated_servers = self.get_designated_servers()
        servers_queried_udp = set(filter(lambda x: x[0] in designated_servers, self._all_servers_clients_queried))
        servers_queried_tcp = set(filter(lambda x: x[0] in designated_servers, self._all_servers_clients_queried_tcp))
        servers_queried = servers_queried_udp.union(servers_queried_tcp)

        unresponsive_udp = servers_queried_udp.difference(self._responsive_servers_clients_udp)
        unresponsive_tcp = servers_queried_tcp.difference(self._responsive_servers_clients_tcp)
        invalid_response = servers_queried.intersection(self._responsive_servers_clients_udp).difference(self._valid_servers_clients)
        not_authoritative = servers_queried.intersection(self._valid_servers_clients).difference(self._auth_servers_clients)

        if unresponsive_udp:
            err = Errors.ServerUnresponsiveUDP()
            for server, client in unresponsive_udp:
                err.add_server_client(server, client, None)
            self.delegation_errors[dns.rdatatype.DS].append(err)

        if unresponsive_tcp:
            err = Errors.ServerUnresponsiveTCP()
            for server, client in unresponsive_tcp:
                err.add_server_client(server, client, None)
            self.delegation_errors[dns.rdatatype.DS].append(err)

        if invalid_response:
            err = Errors.ServerInvalidResponse()
            for server, client in invalid_response:
                err.add_server_client(server, client, None)
            self.delegation_errors[dns.rdatatype.DS].append(err)

        if self.analysis_type == ANALYSIS_TYPE_AUTHORITATIVE:
            if not_authoritative:
                err = Errors.ServerNotAuthoritative()
                for server, client in not_authoritative:
                    err.add_server_client(server, client, None)
                self.delegation_errors[dns.rdatatype.DS].append(err)

    def _populate_negative_response_status(self, query, neg_response_info, \
            bad_soa_error_cls, missing_soa_error_cls, upward_referral_error_cls, missing_nsec_error_cls, \
            nsec_status_cls, nsec3_status_cls, warnings, errors, supported_algs):

        qname_obj = self.get_name(neg_response_info.qname)
        if query.rdtype == dns.rdatatype.DS:
            qname_obj = qname_obj.parent

        soa_owner_name_for_servers = {}
        servers_without_soa = set()
        servers_missing_nsec = set()
        for server, client in neg_response_info.servers_clients:
            for response in neg_response_info.servers_clients[(server, client)]:
                servers_without_soa.add((server, client, response))
                servers_missing_nsec.add((server, client, response))

                self._populate_response_errors(qname_obj, response, server, client, warnings, errors)

        for soa_rrset_info in neg_response_info.soa_rrset_info:
            soa_owner_name = soa_rrset_info.rrset.name

            for server, client in soa_rrset_info.servers_clients:
                for response in soa_rrset_info.servers_clients[(server, client)]:
                    servers_without_soa.remove((server, client, response))
                    soa_owner_name_for_servers[(server,client,response)] = soa_owner_name

            if soa_owner_name != qname_obj.zone.name:
                err = Errors.DomainNameAnalysisError.insert_into_list(bad_soa_error_cls(soa_owner_name=fmt.humanize_name(soa_owner_name), zone_name=fmt.humanize_name(qname_obj.zone.name)), errors, None, None, None)
                if neg_response_info.qname == query.qname:
                    err.servers_clients.update(soa_rrset_info.servers_clients)
                else:
                    for server,client in soa_rrset_info.servers_clients:
                        for response in soa_rrset_info.servers_clients[(server,client)]:
                            if response.recursion_desired_and_available():
                                err.add_server_client(server, client, response)

            self._populate_rrsig_status(query, soa_rrset_info, self.get_name(soa_owner_name), supported_algs, populate_response_errors=False)

        for server,client,response in servers_without_soa:
            if neg_response_info.qname == query.qname or response.recursion_desired_and_available():
                # check for an upward referral
                if upward_referral_error_cls is not None and response.is_upward_referral(qname_obj.zone.name):
                    Errors.DomainNameAnalysisError.insert_into_list(upward_referral_error_cls(), errors, server, client, response)
                else:
                    Errors.DomainNameAnalysisError.insert_into_list(missing_soa_error_cls(), errors, server, client, response)

        if upward_referral_error_cls is not None:
            try:
                index = errors.index(upward_referral_error_cls())
            except ValueError:
                pass
            else:
                upward_referral_error = errors[index]
                for notices in errors, warnings:
                    not_auth_notices = filter(lambda x: isinstance(x, Errors.NotAuthoritative), notices)
                    for notice in not_auth_notices:
                        for server, client in upward_referral_error.servers_clients:
                            for response in upward_referral_error.servers_clients[(server, client)]:
                                notice.remove_server_client(server, client, response)
                        if not notice.servers_clients:
                            notices.remove(notice)

        statuses = []
        status_by_response = {}
        for nsec_set_info in neg_response_info.nsec_set_info:
            if nsec_set_info.use_nsec3:
                status = nsec3_status_cls(neg_response_info.qname, query.rdtype, \
                        soa_owner_name_for_servers.get((server,client,response), qname_obj.zone.name), nsec_set_info)
            else:
                status = nsec_status_cls(neg_response_info.qname, query.rdtype, \
                        soa_owner_name_for_servers.get((server,client,response), qname_obj.zone.name), nsec_set_info)

            for nsec_rrset_info in nsec_set_info.rrsets.values():
                self._populate_rrsig_status(query, nsec_rrset_info, qname_obj, supported_algs, populate_response_errors=False)

            if status.validation_status == Status.NSEC_STATUS_VALID:
                if status not in statuses:
                    statuses.append(status)

            for server, client in nsec_set_info.servers_clients:
                for response in nsec_set_info.servers_clients[(server,client)]:
                    if (server,client,response) in servers_missing_nsec:
                        servers_missing_nsec.remove((server,client,response))
                    if status.validation_status == Status.NSEC_STATUS_VALID:
                        if (server,client,response) in status_by_response:
                            del status_by_response[(server,client,response)]
                    elif neg_response_info.qname == query.qname or response.recursion_desired_and_available():
                        status_by_response[(server,client,response)] = status

        for (server,client,response), status in status_by_response.items():
            if status not in statuses:
                statuses.append(status)

        for server, client, response in servers_missing_nsec:
            # report that no NSEC(3) records were returned
            if qname_obj.zone.signed and (neg_response_info.qname == query.qname or response.recursion_desired_and_available()):
                if response.dnssec_requested():
                    Errors.DomainNameAnalysisError.insert_into_list(missing_nsec_error_cls(), errors, server, client, response)
                elif qname_obj is not None and qname_obj.zone.server_responsive_with_do(server,client,response.effective_tcp):
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.UnableToRetrieveDNSSECRecords(), errors, server, client, response)

        return statuses

    def _populate_nxdomain_status(self, supported_algs):
        self.nxdomain_status = {}
        self.nxdomain_warnings = {}
        self.nxdomain_errors = {}

        _logger.debug('Assessing NXDOMAIN response status of %s...' % (fmt.humanize_name(self.name)))
        for (qname, rdtype), query in self.queries.items():

            for neg_response_info in query.nxdomain_info:
                self.nxdomain_warnings[neg_response_info] = []
                self.nxdomain_errors[neg_response_info] = []
                self.nxdomain_status[neg_response_info] = \
                        self._populate_negative_response_status(query, neg_response_info, \
                                Errors.SOAOwnerNotZoneForNXDOMAIN, Errors.MissingSOAForNXDOMAIN, None, \
                                Errors.MissingNSECForNXDOMAIN, Status.NSECStatusNXDOMAIN, Status.NSEC3StatusNXDOMAIN, \
                                self.nxdomain_warnings[neg_response_info], self.nxdomain_errors[neg_response_info], \
                                supported_algs)

                # check for NOERROR/NXDOMAIN inconsistencies
                if neg_response_info.qname in self.yxdomain and rdtype not in (dns.rdatatype.DS, dns.rdatatype.DLV):
                    for (qname2, rdtype2), query2 in self.queries.items():
                        if rdtype2 in (dns.rdatatype.DS, dns.rdatatype.DLV):
                            continue

                        for rrset_info in filter(lambda x: x.rrset.name == neg_response_info.qname, query2.answer_info):
                            shared_servers_clients = set(rrset_info.servers_clients).intersection(neg_response_info.servers_clients)
                            if shared_servers_clients:
                                err1 = Errors.DomainNameAnalysisError.insert_into_list(Errors.InconsistentNXDOMAIN(qname=neg_response_info.qname, rdtype_nxdomain=dns.rdatatype.to_text(rdtype), rdtype_noerror=dns.rdatatype.to_text(query2.rdtype)), self.nxdomain_warnings[neg_response_info], None, None, None)
                                err2 = Errors.DomainNameAnalysisError.insert_into_list(Errors.InconsistentNXDOMAIN(qname=neg_response_info.qname, rdtype_nxdomain=dns.rdatatype.to_text(rdtype), rdtype_noerror=dns.rdatatype.to_text(query2.rdtype)), self.rrset_warnings[rrset_info], None, None, None)
                                for server, client in shared_servers_clients:
                                    for response in neg_response_info.servers_clients[(server, client)]:
                                        err1.add_server_client(server, client, response)
                                        err2.add_server_client(server, client, response)

                        for neg_response_info2 in filter(lambda x: x.qname == neg_response_info.qname, query2.nodata_info):
                            shared_servers_clients = set(neg_response_info2.servers_clients).intersection(neg_response_info.servers_clients)
                            if shared_servers_clients:
                                err1 = Errors.DomainNameAnalysisError.insert_into_list(Errors.InconsistentNXDOMAIN(qname=neg_response_info.qname, rdtype_nxdomain=dns.rdatatype.to_text(rdtype), rdtype_noerror=dns.rdatatype.to_text(query2.rdtype)), self.nxdomain_warnings[neg_response_info], None, None, None)
                                err2 = Errors.DomainNameAnalysisError.insert_into_list(Errors.InconsistentNXDOMAIN(qname=neg_response_info.qname, rdtype_nxdomain=dns.rdatatype.to_text(rdtype), rdtype_noerror=dns.rdatatype.to_text(query2.rdtype)), self.nodata_warnings[neg_response_info2], None, None, None)
                                for server, client in shared_servers_clients:
                                    for response in neg_response_info.servers_clients[(server, client)]:
                                        err1.add_server_client(server, client, response)
                                        err2.add_server_client(server, client, response)

    def _populate_nodata_status(self, supported_algs):
        self.nodata_status = {}
        self.nodata_warnings = {}
        self.nodata_errors = {}

        _logger.debug('Assessing NODATA response status of %s...' % (fmt.humanize_name(self.name)))
        for (qname, rdtype), query in self.queries.items():

            for neg_response_info in query.nodata_info:
                self.nodata_warnings[neg_response_info] = []
                self.nodata_errors[neg_response_info] = []
                self.nodata_status[neg_response_info] = \
                        self._populate_negative_response_status(query, neg_response_info, \
                                Errors.SOAOwnerNotZoneForNODATA, Errors.MissingSOAForNODATA, Errors.UpwardReferral, \
                                Errors.MissingNSECForNODATA, Status.NSECStatusNoAnswer, Status.NSEC3StatusNoAnswer, \
                                self.nodata_warnings[neg_response_info], self.nodata_errors[neg_response_info], \
                                supported_algs)

    def _populate_dnskey_status(self, trusted_keys):
        if (self.name, dns.rdatatype.DNSKEY) not in self.queries:
            return

        trusted_keys_rdata = set([k for z, k in trusted_keys if z == self.name])
        trusted_keys_existing = set()
        trusted_keys_not_self_signing = set()

        # buid a list of responsive servers
        bailiwick_map, default_bailiwick = self.get_bailiwick_mapping()
        servers_responsive = set()
        for query in self.queries[(self.name, dns.rdatatype.DNSKEY)].queries.values():
            servers_responsive.update([(server,client,query.responses[server][client]) for (server,client) in query.servers_with_valid_complete_response(bailiwick_map, default_bailiwick)])

        # any errors point to their own servers_clients value
        for dnskey in self.get_dnskeys():
            if dnskey.rdata in trusted_keys_rdata:
                trusted_keys_existing.add(dnskey)
                if dnskey not in self.ksks:
                    trusted_keys_not_self_signing.add(dnskey)
            if dnskey in self.revoked_keys and dnskey not in self.ksks:
                err = Errors.RevokedNotSigning()
                err.servers_clients = dnskey.servers_clients
                dnskey.errors.append(err)
            if not self.is_zone():
                err = Errors.DNSKEYNotAtZoneApex(zone=fmt.humanize_name(self.zone.name), name=fmt.humanize_name(self.name))
                err.servers_clients = dnskey.servers_clients
                dnskey.errors.append(err)

            # if there were servers responsive for the query but that didn't return the dnskey
            servers_with_dnskey = set()
            for (server,client) in dnskey.servers_clients:
                for response in dnskey.servers_clients[(server,client)]:
                    servers_with_dnskey.add((server,client,response))
            servers_clients_without = servers_responsive.difference(servers_with_dnskey)
            if servers_clients_without:
                err = Errors.DNSKEYMissingFromServers()
                # if the key is shown to be signing anything other than the
                # DNSKEY RRset, or if it associated with a DS or trust anchor,
                # then mark it as an error; otherwise, mark it as a warning.
                if dnskey in self.zsks or dnskey in self.dnskey_with_ds or dnskey in trusted_keys_existing:
                    dnskey.errors.append(err)
                else:
                    dnskey.warnings.append(err)
                for (server,client,response) in servers_clients_without:
                    err.add_server_client(server, client, response)

        if not trusted_keys_existing.difference(trusted_keys_not_self_signing):
            for dnskey in trusted_keys_not_self_signing:
                err = Errors.TrustAnchorNotSigning()
                err.servers_clients = dnskey.servers_clients
                dnskey.errors.append(err)

    def populate_response_component_status(self, G):
        response_component_status = {}
        for obj in G.node_reverse_mapping:
            if isinstance(obj, (Response.DNSKEYMeta, Response.RRsetInfo, Response.NSECSet, Response.NegativeResponseInfo)):
                node_str = G.node_reverse_mapping[obj]
                status = G.status_for_node(node_str)
                response_component_status[obj] = status

                if isinstance(obj, Response.DNSKEYMeta):
                    for rrset_info in obj.rrset_info:
                        if rrset_info in G.secure_dnskey_rrsets:
                            response_component_status[rrset_info] = Status.RRSET_STATUS_SECURE
                        else:
                            response_component_status[rrset_info] = status

                # Mark each individual NSEC in the set
                elif isinstance(obj, Response.NSECSet):
                    for nsec_name in obj.rrsets:
                        nsec_name_str = nsec_name.canonicalize().to_text().replace(r'"', r'\"')
                        response_component_status[obj.rrsets[nsec_name]] = G.status_for_node(node_str, nsec_name_str)

                elif isinstance(obj, Response.NegativeResponseInfo):
                    # the following two cases are only for zones
                    if G.is_invis(node_str):
                        # A negative response info for a DS query points to the
                        # "top node" of a zone in the graph.  If this "top node" is
                        # colored "insecure", then it indicates that the negative
                        # response has been authenticated.  To reflect this
                        # properly, we change the status to "secure".
                        if obj.rdtype == dns.rdatatype.DS:
                            if status == Status.RRSET_STATUS_INSECURE:
                                if G.secure_nsec_nodes_covering_node(node_str):
                                    response_component_status[obj] = Status.RRSET_STATUS_SECURE

                        # A negative response to a DNSKEY query is a special case.
                        elif obj.rdtype == dns.rdatatype.DNSKEY:
                            # If the "node" was found to be secure, then there must be
                            # a secure entry point into the zone, indicating that there
                            # were other, positive responses to the query (i.e., from
                            # other servers).  That makes this negative response bogus.
                            if status == Status.RRSET_STATUS_SECURE:
                                response_component_status[obj] = Status.RRSET_STATUS_BOGUS

                            # Since the accompanying SOA is not drawn on the graph, we
                            # simply apply the same status to the SOA as is associated
                            # with the negative response.
                            for soa_rrset in obj.soa_rrset_info:
                                response_component_status[soa_rrset] = response_component_status[obj]

                    # for non-DNSKEY responses, verify that the negative
                    # response is secure by checking that the SOA is also
                    # secure (the fact that it is marked "secure" indicates
                    # that the NSEC proof was already authenticated)
                    if obj.rdtype != dns.rdatatype.DNSKEY:
                        # check for secure opt out
                        opt_out_secure = bool(G.secure_nsec3_optout_nodes_covering_node(node_str))
                        if status == Status.RRSET_STATUS_SECURE or \
                                (status == Status.RRSET_STATUS_INSECURE and opt_out_secure):
                            soa_secure = False
                            for soa_rrset in obj.soa_rrset_info:
                                if G.status_for_node(G.node_reverse_mapping[soa_rrset]) == Status.RRSET_STATUS_SECURE:
                                    soa_secure = True
                            if not soa_secure:
                                response_component_status[obj] = Status.RRSET_STATUS_BOGUS

        self._set_response_component_status(response_component_status)

    def _set_response_component_status(self, response_component_status, is_dlv=False, trace=None, follow_mx=True):
        if trace is None:
            trace = []

        # avoid loops
        if self in trace:
            return

        # populate status of dependencies
        for cname in self.cname_targets:
            for target, cname_obj in self.cname_targets[cname].items():
                if cname_obj is not None:
                    cname_obj._set_response_component_status(response_component_status, trace=trace + [self])
        if follow_mx:
            for target, mx_obj in self.mx_targets.items():
                if mx_obj is not None:
                    mx_obj._set_response_component_status(response_component_status, trace=trace + [self], follow_mx=False)
        for signer, signer_obj in self.external_signers.items():
            if signer_obj is not None:
                signer_obj._set_response_component_status(response_component_status, trace=trace + [self])
        for target, ns_obj in self.ns_dependencies.items():
            if ns_obj is not None:
                ns_obj._set_response_component_status(response_component_status, trace=trace + [self])

        # populate status of ancestry
        if self.parent is not None:
            self.parent._set_response_component_status(response_component_status, trace=trace + [self])
        if self.dlv_parent is not None:
            self.dlv_parent._set_response_component_status(response_component_status, is_dlv=True, trace=trace + [self])

        self.response_component_status = response_component_status

    def _serialize_rrset_info(self, rrset_info, consolidate_clients=False, show_servers=True, loglevel=logging.DEBUG, html_format=False):
        d = collections.OrderedDict()

        rrsig_list = []
        if self.rrsig_status[rrset_info]:
            rrsigs = self.rrsig_status[rrset_info].keys()
            rrsigs.sort()
            for rrsig in rrsigs:
                dnskeys = self.rrsig_status[rrset_info][rrsig].keys()
                dnskeys.sort()
                for dnskey in dnskeys:
                    rrsig_status = self.rrsig_status[rrset_info][rrsig][dnskey]
                    rrsig_serialized = rrsig_status.serialize(consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                    if rrsig_serialized:
                        rrsig_list.append(rrsig_serialized)

        dname_list = []
        if rrset_info in self.dname_status:
            for dname_status in self.dname_status[rrset_info]:
                dname_serialized = dname_status.serialize(self._serialize_rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if dname_serialized:
                    dname_list.append(dname_serialized)

        wildcard_proof_list = collections.OrderedDict()
        if rrset_info.wildcard_info:
            wildcard_names = rrset_info.wildcard_info.keys()
            wildcard_names.sort()
            for wildcard_name in wildcard_names:
                wildcard_name_str = wildcard_name.canonicalize().to_text()
                wildcard_proof_list[wildcard_name_str] = []
                for nsec_status in self.wildcard_status[rrset_info.wildcard_info[wildcard_name]]:
                    nsec_serialized = nsec_status.serialize(self._serialize_rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                    if nsec_serialized:
                        wildcard_proof_list[wildcard_name_str].append(nsec_serialized)
                if not wildcard_proof_list[wildcard_name_str]:
                    del wildcard_proof_list[wildcard_name_str]

        show_info = loglevel <= logging.INFO or \
                (self.rrset_warnings[rrset_info] and loglevel <= logging.WARNING) or \
                (self.rrset_errors[rrset_info] and loglevel <= logging.ERROR) or \
                (rrsig_list or dname_list or wildcard_proof_list)

        if show_info:
            if rrset_info.rrset.rdtype == dns.rdatatype.NSEC3:
                d['id'] = '%s/%s/%s' % (fmt.format_nsec3_name(rrset_info.rrset.name), dns.rdataclass.to_text(rrset_info.rrset.rdclass), dns.rdatatype.to_text(rrset_info.rrset.rdtype))
            else:
                d['id'] = '%s/%s/%s' % (rrset_info.rrset.name.canonicalize().to_text(), dns.rdataclass.to_text(rrset_info.rrset.rdclass), dns.rdatatype.to_text(rrset_info.rrset.rdtype))

        if loglevel <= logging.DEBUG:
            d['description'] = unicode(rrset_info)
            d.update(rrset_info.serialize(include_rrsig_info=False, consolidate_clients=consolidate_clients, show_servers=show_servers, html_format=html_format))

        if rrsig_list:
            d['rrsig'] = rrsig_list

        if dname_list:
            d['dname'] = dname_list

        if wildcard_proof_list:
            d['wildcard_proof'] = wildcard_proof_list

        if show_info and self.response_component_status is not None:
            d['status'] = Status.rrset_status_mapping[self.response_component_status[rrset_info]]

        if self.rrset_warnings[rrset_info] and loglevel <= logging.WARNING:
            d['warnings'] = [w.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for w in self.rrset_warnings[rrset_info]]

        if self.rrset_errors[rrset_info] and loglevel <= logging.ERROR:
            d['errors'] = [e.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for e in self.rrset_errors[rrset_info]]

        return d

    def _serialize_negative_response_info(self, neg_response_info, neg_status, warnings, errors, consolidate_clients=False, loglevel=logging.DEBUG, html_format=False):
        d = collections.OrderedDict()

        proof_list = []
        for nsec_status in neg_status[neg_response_info]:
            nsec_serialized = nsec_status.serialize(self._serialize_rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
            if nsec_serialized:
                proof_list.append(nsec_serialized)

        soa_list = []
        for soa_rrset_info in neg_response_info.soa_rrset_info:
            rrset_serialized = self._serialize_rrset_info(soa_rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
            if rrset_serialized:
                soa_list.append(rrset_serialized)

        show_info = loglevel <= logging.INFO or \
                (warnings[neg_response_info] and loglevel <= logging.WARNING) or \
                (errors[neg_response_info] and loglevel <= logging.ERROR) or \
                (proof_list or soa_list)

        if show_info:
            d['id'] = '%s/%s/%s' % (neg_response_info.qname.canonicalize().to_text(), 'IN', dns.rdatatype.to_text(neg_response_info.rdtype))

        if proof_list:
            d['proof'] = proof_list

        if soa_list:
            d['soa'] = soa_list

        if show_info and self.response_component_status is not None:
            d['status'] = Status.rrset_status_mapping[self.response_component_status[neg_response_info]]

        if loglevel <= logging.DEBUG or \
                (warnings[neg_response_info] and loglevel <= logging.WARNING) or \
                (errors[neg_response_info] and loglevel <= logging.ERROR):
            servers = tuple_to_dict(neg_response_info.servers_clients)
            if consolidate_clients:
                servers = list(servers)
                servers.sort()
            d['servers'] = servers

        if warnings[neg_response_info] and loglevel <= logging.WARNING:
            d['warnings'] = [w.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for w in warnings[neg_response_info]]

        if errors[neg_response_info] and loglevel <= logging.ERROR:
            d['errors'] = [e.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for e in errors[neg_response_info]]

        return d

    def _serialize_query_status(self, query, consolidate_clients=False, loglevel=logging.DEBUG, html_format=False):
        d = collections.OrderedDict()
        d['answer'] = []
        d['nxdomain'] = []
        d['nodata'] = []
        d['error'] = []

        for rrset_info in query.answer_info:
            if rrset_info.rrset.name == query.qname or self.analysis_type == ANALYSIS_TYPE_RECURSIVE:
                rrset_serialized = self._serialize_rrset_info(rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if rrset_serialized:
                    d['answer'].append(rrset_serialized)

        for neg_response_info in query.nxdomain_info:
            # only look at qname
            if neg_response_info.qname == query.qname or self.analysis_type == ANALYSIS_TYPE_RECURSIVE:
                neg_response_serialized = self._serialize_negative_response_info(neg_response_info, self.nxdomain_status, self.nxdomain_warnings, self.nxdomain_errors, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if neg_response_serialized:
                    d['nxdomain'].append(neg_response_serialized)

        for neg_response_info in query.nodata_info:
            # only look at qname
            if neg_response_info.qname == query.qname or self.analysis_type == ANALYSIS_TYPE_RECURSIVE:
                neg_response_serialized = self._serialize_negative_response_info(neg_response_info, self.nodata_status, self.nodata_warnings, self.nodata_errors, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if neg_response_serialized:
                    d['nodata'].append(neg_response_serialized)

        for error in self.response_errors[query]:
            error_serialized = error.serialize(consolidate_clients=consolidate_clients, html_format=html_format)
            if error_serialized:
                d['error'].append(error_serialized)

        if not d['answer']: del d['answer']
        if not d['nxdomain']: del d['nxdomain']
        if not d['nodata']: del d['nodata']
        if not d['error']: del d['error']

        return d

    def _serialize_dnskey_status(self, consolidate_clients=False, loglevel=logging.DEBUG, html_format=False):
        d = []

        for dnskey in self.get_dnskeys():
            dnskey_serialized = dnskey.serialize(consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
            if dnskey_serialized:
                if self.response_component_status is not None:
                    dnskey_serialized['status'] = Status.rrset_status_mapping[self.response_component_status[dnskey]]
                d.append(dnskey_serialized)

        return d

    def _serialize_delegation_status(self, rdtype, consolidate_clients=False, loglevel=logging.DEBUG, html_format=False):
        d = collections.OrderedDict()

        dss = self.ds_status_by_ds[rdtype].keys()
        d['ds'] = []
        dss.sort()
        for ds in dss:
            dnskeys = self.ds_status_by_ds[rdtype][ds].keys()
            dnskeys.sort()
            for dnskey in dnskeys:
                ds_status = self.ds_status_by_ds[rdtype][ds][dnskey]
                ds_serialized = ds_status.serialize(consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if ds_serialized:
                    d['ds'].append(ds_serialized)
        if not d['ds']:
            del d['ds']

        try:
            neg_response_info = filter(lambda x: x.qname == self.name and x.rdtype == rdtype, self.nodata_status)[0]
            status = self.nodata_status
        except IndexError:
            try:
                neg_response_info = filter(lambda x: x.qname == self.name and x.rdtype == rdtype, self.nxdomain_status)[0]
                status = self.nxdomain_status
            except IndexError:
                neg_response_info = None

        if neg_response_info is not None:
            d['insecurity_proof'] = []
            for nsec_status in status[neg_response_info]:
                nsec_serialized = nsec_status.serialize(self._serialize_rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if nsec_serialized:
                    d['insecurity_proof'].append(nsec_serialized)
            if not d['insecurity_proof']:
                del d['insecurity_proof']

        if loglevel <= logging.INFO or self.delegation_status[rdtype] not in (Status.DELEGATION_STATUS_SECURE, Status.DELEGATION_STATUS_INSECURE):
            d['status'] = Status.delegation_status_mapping[self.delegation_status[rdtype]]

        if self.delegation_warnings[rdtype] and loglevel <= logging.WARNING:
            d['warnings'] = [w.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for w in self.delegation_warnings[rdtype]]

        if self.delegation_errors[rdtype] and loglevel <= logging.ERROR:
            d['errors'] = [e.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for e in self.delegation_errors[rdtype]]

        return d

    def serialize_status(self, d=None, is_dlv=False, loglevel=logging.DEBUG, ancestry_only=False, level=RDTYPES_ALL, trace=None, follow_mx=True, html_format=False):
        if d is None:
            d = collections.OrderedDict()

        if trace is None:
            trace = []

        # avoid loops
        if self in trace:
            return d

        # if we're a stub, there's no status to serialize
        if self.stub:
            return d

        name_str = self.name.canonicalize().to_text()
        if name_str in d:
            return d

        cname_ancestry_only = self.analysis_type == ANALYSIS_TYPE_RECURSIVE

        # serialize status of dependencies first because their version of the
        # analysis might be the most complete (considering re-dos)
        if level <= self.RDTYPES_NS_TARGET:
            for cname in self.cname_targets:
                for target, cname_obj in self.cname_targets[cname].items():
                    if cname_obj is not None:
                        cname_obj.serialize_status(d, loglevel=loglevel, ancestry_only=cname_ancestry_only, level=max(self.RDTYPES_ALL_SAME_NAME, level), trace=trace + [self], html_format=html_format)
            if follow_mx:
                for target, mx_obj in self.mx_targets.items():
                    if mx_obj is not None:
                        mx_obj.serialize_status(d, loglevel=loglevel, level=max(self.RDTYPES_ALL_SAME_NAME, level), trace=trace + [self], follow_mx=False, html_format=html_format)
        if level <= self.RDTYPES_SECURE_DELEGATION:
            for signer, signer_obj in self.external_signers.items():
                signer_obj.serialize_status(d, loglevel=loglevel, level=self.RDTYPES_SECURE_DELEGATION, trace=trace + [self], html_format=html_format)
            for target, ns_obj in self.ns_dependencies.items():
                if ns_obj is not None:
                    ns_obj.serialize_status(d, loglevel=loglevel, level=self.RDTYPES_NS_TARGET, trace=trace + [self], html_format=html_format)

        # serialize status of ancestry
        if level <= self.RDTYPES_SECURE_DELEGATION:
            if self.parent is not None:
                self.parent.serialize_status(d, loglevel=loglevel, level=self.RDTYPES_SECURE_DELEGATION, trace=trace + [self], html_format=html_format)
            if self.dlv_parent is not None:
                self.dlv_parent.serialize_status(d, is_dlv=True, loglevel=loglevel, level=self.RDTYPES_SECURE_DELEGATION, trace=trace + [self], html_format=html_format)

        # if we're only looking for the secure ancestry of a name, and not the
        # name itself (i.e., because this is a subsequent name in a CNAME
        # chain)
        if ancestry_only:

            # only proceed if the name is a zone (and thus as DNSKEY, DS, etc.)
            if not self.is_zone():
                return d

            # explicitly set the level to self.RDTYPES_SECURE_DELEGATION, so
            # the other query types aren't retrieved.
            level = self.RDTYPES_SECURE_DELEGATION

        consolidate_clients = self.single_client()

        d[name_str] = collections.OrderedDict()
        if loglevel <= logging.INFO or self.status not in (Status.NAME_STATUS_NOERROR, Status.NAME_STATUS_NXDOMAIN):
            d[name_str]['status'] = Status.name_status_mapping[self.status]

        d[name_str]['queries'] = collections.OrderedDict()
        query_keys = self.queries.keys()
        query_keys.sort()
        required_rdtypes = self._rdtypes_for_analysis_level(level)

        # don't serialize NS data in names for which delegation-only
        # information is required
        if level >= self.RDTYPES_SECURE_DELEGATION:
            required_rdtypes.difference_update([self.referral_rdtype, dns.rdatatype.NS])

        for (qname, rdtype) in query_keys:

            if level > self.RDTYPES_ALL and qname not in (self.name, self.dlv_name):
                continue

            if required_rdtypes is not None and rdtype not in required_rdtypes:
                continue

            query_serialized = self._serialize_query_status(self.queries[(qname, rdtype)], consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
            if query_serialized:
                qname_type_str = '%s/%s/%s' % (qname.canonicalize().to_text(), dns.rdataclass.to_text(dns.rdataclass.IN), dns.rdatatype.to_text(rdtype))
                d[name_str]['queries'][qname_type_str] = query_serialized

        if not d[name_str]['queries']:
            del d[name_str]['queries']

        if level <= self.RDTYPES_SECURE_DELEGATION and (self.name, dns.rdatatype.DNSKEY) in self.queries:
            dnskey_serialized = self._serialize_dnskey_status(consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
            if dnskey_serialized:
                d[name_str]['dnskey'] = dnskey_serialized

        if self.is_zone():
            if self.parent is not None and not is_dlv:
                delegation_serialized = self._serialize_delegation_status(dns.rdatatype.DS, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if delegation_serialized:
                    d[name_str]['delegation'] = delegation_serialized

            if self.dlv_parent is not None:
                if (self.dlv_name, dns.rdatatype.DLV) in self.queries:
                    delegation_serialized = self._serialize_delegation_status(dns.rdatatype.DLV, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                    if delegation_serialized:
                        d[name_str]['dlv'] = delegation_serialized

        if not d[name_str]:
            del d[name_str]

        return d

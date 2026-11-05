dSIPRouter API Intro
====================

The complete API is defined as a public Postman Workspace, which can be found `here <https://www.postman.com/dopensource/workspace/dsiprouter/collection/4319695-9c09dea3-0b4b-4a20-a615-fb8fc16811af?action=share&creator=4319695>`_ 

The steps to obtain the API Token key and examples of using the API via curl are below, but we highly recommend using Postman for testing the API.

Getting Your Token
------------------

Your token was provided to you after you installed dSIPRouter.  You can reset your token if you didn't write it down, by executing the following command

.. code-block:: bash

    DSIP_HOSTNAME=<your ip or hostname>
    DSIP_TOKEN=<your token>
    dsiprouter setcredentials -ac $DSIP_TOKEN

Executing Kamailio stats API
----------------------------

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" -X GET https://$DSIP_HOSTNAME:5000/api/v1/kamailio/stats

Executing Lease Point API
-------------------------

Create a new endpoint lease

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" -H "Content-Type: application/json" -X GET "https://$DSIP_HOSTNAME:5000/api/v1/endpoint/lease?ttl=15&email=mack@dsiprouter.org"

Revoking and replacing with your own lease ID

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" -H "Content-Type: application/json" -X PUT "https://$DSIP_HOSTNAME:5000/api/v1/endpoint/lease/1/revoke"

Executing Outbound Routes API
-----------------------------

List all outbound routes (both simple and LCR):

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" -X GET https://$DSIP_HOSTNAME:5000/api/v1/outboundroutes

Create a simple outbound route (matches a To-Prefix to a carrier group):

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" -H "Content-Type: application/json" \
         -X POST -d '{"name":"My Route","prefix":"1","gwgroupid":"2"}' \
         https://$DSIP_HOSTNAME:5000/api/v1/outboundroutes

Create an LCR (From-Prefix) outbound route. The presence of ``from_prefix`` promotes the rule to LCR routing and auto-allocates a dynamic groupid in ``[FLT_LCR_MIN, FLT_FWD_MIN)``:

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" -H "Content-Type: application/json" \
         -X POST -d '{"name":"313 to ATT","from_prefix":"313","prefix":"1","gwgroupid":"2"}' \
         https://$DSIP_HOSTNAME:5000/api/v1/outboundroutes

Update a route (any subset of fields). Sending ``"from_prefix": ""`` demotes an LCR route back to simple:

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" -H "Content-Type: application/json" \
         -X PUT -d '{"priority":5}' https://$DSIP_HOSTNAME:5000/api/v1/outboundroutes/<ruleid>

Delete a route (paired ``dsip_lcr`` row, if any, is removed automatically):

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" -X DELETE https://$DSIP_HOSTNAME:5000/api/v1/outboundroutes/<ruleid>

Listing Numbers
---------------

``GET /api/numbers/v1/number`` returns a bounded page. The default is 25
records; use ``start`` (zero-based offset) and ``length`` (1–500) to retrieve
other pages. Larger lengths are capped at 500; ``length=-1`` is not supported.
Clients that previously expected the entire inventory must now iterate pages.

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" \
         "https://$DSIP_HOSTNAME:5000/api/numbers/v1/number?start=25&length=25"

The response retains ``data``, ``msg``, ``error``, and reload flags, and adds
``start``, ``length``, ``recordsTotal`` (inventory size), ``recordsFiltered``
(matches before pagination), and ``draw`` (the supplied DataTables request
counter, or zero). An empty page still includes these counts.

Existing filters are supported: ``source`` matches the carrier, ``pool`` and
``status`` match exactly, and ``starts_with``, ``contains``, and ``ends_with``
match the DID. ``search[value]`` searches the displayed columns, matching each
whitespace-separated term. Search text is literal, not SQL wildcards or regex.

DataTables ordering uses ``order[0][column]`` and ``order[0][dir]`` (``asc`` or
``desc``); additional ordered columns use indices 1, 2, etc. Column IDs are:
1 = ID, 2 = DID, 3 = status, 4 = carrier, 5 = pool, 6 = assigned date,
7 = assigned length, 8 = assigned reference ID. The default order is ID
ascending, with ID also used to break ties. Invalid paging or ordering returns
HTTP 400. The Numbers UI uses this protocol for paging, searching and sorting.

Further Reading
+++++++++++++++

All available routes are documented in the :doc:`routes documentation <../routes/index>`.

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

Executing Inbound Routes API
---------------------------

``/api/v1/inboundmapping`` supports the options in the Inbound Routes add/edit
dialogs, including endpoint groups, load balancing, and hard/failover forwarding.
It uses the existing API token or logged-in session authentication.

List routes with GET, or select one with ``?ruleid=<id>`` or ``?did=<DID>``.
POST creates a route; PUT updates only supplied fields; DELETE removes a route
and its forwarding records and dedicated forwarding routes. PUT and DELETE
require exactly one query-string selector. Unknown routes return HTTP 404 and
invalid fields or conflicting selectors return HTTP 400.

The JSON body accepts these fields:

.. list-table:: Inbound route fields
   :header-rows: 1
   :widths: 25 75

   * - Field
     - Meaning
   * - ``did``
     - Incoming DID or pattern. Required on POST; an empty string is a catch-all.
   * - ``name``
     - Friendly route name. Commas are reserved by the stored metadata format.
   * - ``gwgroupid``
     - Endpoint-group ID. Empty string, null, or 0 means unassigned. Omitting
       both ``gwgroupid`` and ``servers`` on POST also creates an unassigned route.
   * - ``servers``
     - Existing alternative to ``gwgroupid``: one or two gateway IDs, such as
       ``["66", "67"]``, or group references such as ``["#22"]``. Do not send both fields.
   * - ``lb_enabled``
     - Boolean (or 0/1). Enables load balancing for a single group destination.
       The UI dropdown values ``lb_<groupid>_<dispatcher-setid>`` are also accepted
       as ``gwgroupid`` and imply true. As in the UI, both LB dropdown variants
       store the group ID and LB flag. Changing LB mode updates other inbound
       routes using the same destination group.
   * - ``hardfwd_enabled`` / ``failfwd_enabled``
     - Boolean (or 0/1). Enable or disable the corresponding forwarding behavior.
   * - ``hf_fwddid`` / ``ff_fwddid``
     - Forwarded DID. Empty means leave the called number unchanged.
   * - ``hf_gwgroupid`` / ``ff_gwgroupid``
     - Optional carrier/endpoint group for forwarding. Empty string, null, or 0
       uses normal outbound routing for the forwarded DID.

Create a route with both forwarding options:

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" -H "Content-Type: application/json" \
         -X POST -d '{"did":"13135551234","name":"Support","gwgroupid":"22",
                     "hardfwd_enabled":true,"hf_fwddid":"13135559876","hf_gwgroupid":"10",
                     "failfwd_enabled":true,"ff_fwddid":"13135554321"}' \
         https://$DSIP_HOSTNAME:5000/api/v1/inboundmapping

Use group IDs that exist on your installation. The API allocates forwarding
rule/group IDs; callers do not supply the hidden IDs used by the HTML form.
Forwarding fields default to disabled on POST. On PUT, omitted fields preserve
their current values; to configure a previously disabled forward, also send its
enabled flag. Sending false removes its forwarding configuration.

Update only the failover destination, retaining all other route settings:

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" -H "Content-Type: application/json" \
         -X PUT -d '{"ff_fwddid":"13135550000"}' \
         "https://$DSIP_HOSTNAME:5000/api/v1/inboundmapping?ruleid=123"

Disable hard forwarding:

.. code-block:: bash

    curl -k -H "Authorization: Bearer $DSIP_TOKEN" -H "Content-Type: application/json" \
         -X PUT -d '{"hardfwd_enabled":false}' \
         "https://$DSIP_HOSTNAME:5000/api/v1/inboundmapping?ruleid=123"

GET, POST, and PUT return route objects in ``data`` with the existing ``ruleid``,
``did``, ``name``, and ``servers`` fields plus ``gwgroupid``, ``lb_enabled``, and
all six forwarding fields. Forwarding flags are booleans and group IDs are
strings. Routes created through the UI can be read and edited through this API.
Selectors are separate from the JSON body, so a PUT to ``?did=<old>`` with
``{"did":"<new>"}`` renames the selected route. Number-inventory status is
synchronized when the route is assigned, unassigned, renamed, or deleted.

Successful mutations return ``kamreload: true``. Reload Kamailio to activate
the saved changes, just as when saving through the UI. Database changes,
forwarding changes, and number-status changes commit together or roll back
together if validation or persistence fails.

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

Further Reading
+++++++++++++++

All available routes are documented in the :doc:`routes documentation <../routes/index>`.

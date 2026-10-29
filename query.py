"""The one GraphQL document this plugin sends.

``updatedAt`` is selected so the morning list can group issues that have not
moved. The comparison itself is done here, after the pages are read, because
the list also needs issues that moved recently. ``schema/schema.graphql`` is
what the tests check this document against.
"""

PAGE_SIZE = 50

ASSIGNED_QUERY = """query AssignedOpen($after: String) {
  viewer {
    assignedIssues(
      first: 50
      after: $after
      includeArchived: false
      filter: { state: { type: { nin: ["completed", "canceled", "duplicate"] } } }
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        identifier
        title
        url
        dueDate
        updatedAt
        state {
          name
          type
        }
      }
    }
  }
}
"""

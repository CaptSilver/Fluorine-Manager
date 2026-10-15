#include <uibase/exceptions.h>
#include <uibase/nxmurl.h>

#include <gtest/gtest.h>

#include <cstring>
#include <new>

namespace
{
// Builds an NXMUrl in memory pre-filled with 0xFF. A member the constructor
// forgets to initialise then reads back as -1 / true instead of an accidental
// zero, which makes the missing-initialiser bug fail every run.
//
// This is a deliberate trick, not a pattern to copy: reading members the constructor
// did not initialise is undefined behaviour on paper. It works because the NXMUrl
// constructor is defined out of line in libuibase.so, so the compiler cannot see
// the constructor body and cannot assume anything about the bytes we pre-filled.
class DirtyNxmUrl
{
public:
  explicit DirtyNxmUrl(const QString& link)
  {
    std::memset(m_storage, 0xFF, sizeof(m_storage));
    m_url = new (m_storage) NXMUrl(link);
  }

  ~DirtyNxmUrl() { m_url->~NXMUrl(); }

  DirtyNxmUrl(const DirtyNxmUrl&)            = delete;
  DirtyNxmUrl& operator=(const DirtyNxmUrl&) = delete;

  const NXMUrl* operator->() const { return m_url; }

private:
  alignas(NXMUrl) unsigned char m_storage[sizeof(NXMUrl)];
  NXMUrl* m_url = nullptr;
};
}  // namespace

TEST(NXMUrl, CollectionUrlReportsZeroModAndFileIds)
{
  const DirtyNxmUrl url("nxm://cyberpunk2077/collections/rcuccp/revisions/189");

  EXPECT_TRUE(url->isCollection());
  EXPECT_EQ(url->collectionId(), QStringLiteral("rcuccp"));
  EXPECT_EQ(url->collectionRevision(), 189);
  EXPECT_EQ(url->game(), QStringLiteral("cyberpunk2077"));
  EXPECT_EQ(url->modId(), 0);
  EXPECT_EQ(url->fileId(), 0);
  EXPECT_EQ(url->expires(), 0);
  EXPECT_EQ(url->userId(), 0);
  EXPECT_TRUE(url->key().isEmpty());
}

TEST(NXMUrl, CollectionSlugWithDashAndUnderscoreIsCollection)
{
  const NXMUrl dashed("nxm://skyrimspecialedition/collections/ncr-core/revisions/12");
  EXPECT_TRUE(dashed.isCollection());
  EXPECT_EQ(dashed.collectionId(), QStringLiteral("ncr-core"));
  EXPECT_EQ(dashed.collectionRevision(), 12);

  const NXMUrl underscored("nxm://skyrimspecialedition/collections/my_list_2/revisions/3");
  EXPECT_TRUE(underscored.isCollection());
  EXPECT_EQ(underscored.collectionId(), QStringLiteral("my_list_2"));
  EXPECT_EQ(underscored.collectionRevision(), 3);
}

TEST(NXMUrl, ModUrlIsNotCollection)
{
  const DirtyNxmUrl url(
      "nxm://cyberpunk2077/mods/28966/files/158871?key=abc&expires=1700000000&user_id=42");

  EXPECT_FALSE(url->isCollection());
  EXPECT_EQ(url->modId(), 28966);
  EXPECT_EQ(url->fileId(), 158871);
  EXPECT_EQ(url->expires(), 1700000000);
  EXPECT_EQ(url->userId(), 42);
  EXPECT_EQ(url->key(), QStringLiteral("abc"));
  EXPECT_EQ(url->collectionRevision(), 0);
  EXPECT_TRUE(url->collectionId().isEmpty());

  EXPECT_THROW(NXMUrl("nxm://cyberpunk2077/nothing/here"),
               MOBase::InvalidNXMLinkException);
}
